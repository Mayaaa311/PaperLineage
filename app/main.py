from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from difflib import SequenceMatcher

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import Session

from .conference_scraper import scrape_conference_websites
from .db import Base, engine, get_db
from .llm import explain_trace_edge
from .models import (
    Favorite,
    Paper,
    PaperAnalysis,
    PaperDetailCache,
    SavedSearch,
    SavedSearchPaper,
    TraceGraphEdge,
    TraceGraphNode,
    TraceRequest,
)
from .paper_analysis import get_or_create_paper_analysis
from .scholar import ScholarClient, normalize_paper
from .trace import run_trace_job
from .utils import load_authors, load_json_list, paper_to_output, upsert_paper
from . import schemas

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="Paper Reading Trace App", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

Base.metadata.create_all(bind=engine)


def _run_lightweight_migrations() -> None:
    # Minimal SQLite migration support for newly added columns.
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.begin() as conn:
        paper_cols = conn.execute(text("PRAGMA table_info('papers')")).fetchall()
        paper_names = {str(row[1]) for row in paper_cols}
        if "review_score_avg" not in paper_names:
            conn.execute(text("ALTER TABLE papers ADD COLUMN review_score_avg FLOAT"))
        if "review_count" not in paper_names:
            conn.execute(text("ALTER TABLE papers ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0"))
        if "decision" not in paper_names:
            conn.execute(text("ALTER TABLE papers ADD COLUMN decision VARCHAR"))

        cols = conn.execute(text("PRAGMA table_info('paper_analyses')")).fetchall()
        names = {str(row[1]) for row in cols}
        if "limitations_json" not in names:
            conn.execute(
                text(
                    "ALTER TABLE paper_analyses ADD COLUMN limitations_json TEXT NOT NULL DEFAULT '[]'"
                )
            )
        if "dataset_dependencies_json" not in names:
            conn.execute(
                text(
                    "ALTER TABLE paper_analyses ADD COLUMN dataset_dependencies_json TEXT NOT NULL DEFAULT '[]'"
                )
            )


_run_lightweight_migrations()

CONFERENCE_ALIASES: dict[str, list[str]] = {
    "ICLR": ["ICLR", "INTERNATIONAL CONFERENCE ON LEARNING REPRESENTATIONS"],
    "NEURIPS": ["NEURIPS", "NIPS", "NEURAL INFORMATION PROCESSING SYSTEMS"],
    "ICML": ["ICML", "INTERNATIONAL CONFERENCE ON MACHINE LEARNING"],
    "CVPR": ["CVPR", "CONFERENCE ON COMPUTER VISION AND PATTERN RECOGNITION"],
    "ECCV": ["ECCV", "EUROPEAN CONFERENCE ON COMPUTER VISION"],
    "ACL": ["ACL", "ANNUAL MEETING OF THE ASSOCIATION FOR COMPUTATIONAL LINGUISTICS"],
    "EMNLP": ["EMNLP", "EMPIRICAL METHODS IN NATURAL LANGUAGE PROCESSING"],
    "KDD": ["KDD", "KNOWLEDGE DISCOVERY AND DATA MINING"],
}


def matches_conference(venue: str | None, conference_filters: list[str]) -> bool:
    if not conference_filters:
        return True
    if not venue:
        return False
    venue_upper = venue.upper()
    for conf in conference_filters:
        alias_candidates = CONFERENCE_ALIASES.get(conf.upper(), [conf.upper()])
        if any(alias in venue_upper for alias in alias_candidates):
            return True
    return False


def build_search_key(
    search_mode: str,
    query_text: str,
    conferences: list[str],
    start_year: int | None,
    end_year: int | None,
    max_results: int,
) -> str:
    normalized = {
        "search_mode": search_mode,
        "query_text": query_text.strip().lower(),
        "conferences": sorted({c.strip().upper() for c in conferences if c.strip()}),
        "start_year": start_year,
        "end_year": end_year,
        "max_results": max_results,
    }
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode("utf-8")).hexdigest()


def dedupe_papers(papers: list[Paper]) -> list[Paper]:
    out: list[Paper] = []
    seen: set[str] = set()
    for paper in papers:
        if paper.id in seen:
            continue
        seen.add(paper.id)
        out.append(paper)
    return out


def _normalized_title(text: str | None) -> str:
    return " ".join([x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if x])


def _title_similarity(a: str | None, b: str | None) -> float:
    na = _normalized_title(a)
    nb = _normalized_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def enrich_missing_citations(
    db: Session,
    papers: list[Paper],
    max_lookups: int = 80,
) -> None:
    scholar = ScholarClient()
    lookups = 0
    cache: dict[str, dict | None] = {}
    for paper in papers:
        if lookups >= max_lookups:
            break
        if (paper.citation_count or 0) > 0:
            continue
        title = (paper.title or "").strip()
        if not title:
            continue
        if title in cache:
            best = cache[title]
        else:
            best = None
            try:
                candidates = scholar.search_papers(query=title, limit=8)
            except Exception:
                candidates = []
            best_score = 0.0
            for raw in candidates:
                normalized = normalize_paper(raw)
                cand_title = normalized.get("title")
                if not cand_title:
                    continue
                sim = _title_similarity(title, cand_title)
                year_bonus = 0.0
                if paper.year and normalized.get("year"):
                    try:
                        if abs(int(paper.year) - int(normalized["year"])) <= 1:
                            year_bonus = 0.08
                    except Exception:
                        year_bonus = 0.0
                score = sim + year_bonus
                if score > best_score:
                    best_score = score
                    best = normalized
            if best and best_score < 0.84:
                best = None
            cache[title] = best
            lookups += 1

        if not best:
            continue
        if best.get("citation_count"):
            paper.citation_count = max(paper.citation_count or 0, best.get("citation_count") or 0)
        if best.get("url") and not paper.url:
            paper.url = best.get("url")
        if best.get("venue") and not paper.venue:
            paper.venue = best.get("venue")
        if best.get("review_score_avg") is not None and paper.review_score_avg is None:
            paper.review_score_avg = best.get("review_score_avg")
        if best.get("review_count") and (paper.review_count or 0) == 0:
            paper.review_count = best.get("review_count") or 0
        if best.get("decision") and not paper.decision:
            paper.decision = best.get("decision")
    db.flush()


def load_saved_search_papers(db: Session, user_id: str, search_key: str) -> list[Paper]:
    saved = db.execute(
        select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.search_key == search_key)
    ).scalar_one_or_none()
    if not saved:
        return []

    ordered_ids = list(
        db.execute(
            select(SavedSearchPaper.paper_id)
            .where(SavedSearchPaper.saved_search_id == saved.id)
            .order_by(SavedSearchPaper.rank.asc())
        ).scalars()
    )
    if not ordered_ids:
        return []

    rows = db.execute(select(Paper).where(Paper.id.in_(ordered_ids))).scalars().all()
    by_id = {p.id: p for p in rows}
    return [by_id[x] for x in ordered_ids if x in by_id]


def save_search_snapshot(
    db: Session,
    user_id: str,
    search_key: str,
    search_mode: str,
    query_text: str,
    conferences: list[str],
    start_year: int | None,
    end_year: int | None,
    max_results: int,
    paper_ids: list[str],
) -> int:
    existing = db.execute(
        select(SavedSearch).where(SavedSearch.user_id == user_id, SavedSearch.search_key == search_key)
    ).scalar_one_or_none()
    conferences_json = json.dumps(conferences)

    if not existing:
        existing = SavedSearch(
            user_id=user_id,
            search_key=search_key,
            search_mode=search_mode,
            query_text=query_text,
            conferences_json=conferences_json,
            start_year=start_year,
            end_year=end_year,
            max_results=max_results,
        )
        db.add(existing)
        db.flush()
    else:
        existing.search_mode = search_mode
        existing.query_text = query_text
        existing.conferences_json = conferences_json
        existing.start_year = start_year
        existing.end_year = end_year
        existing.max_results = max_results
        db.flush()

    db.execute(delete(SavedSearchPaper).where(SavedSearchPaper.saved_search_id == existing.id))
    clean_ids = [x for x in paper_ids if x]
    if clean_ids:
        existing_paper_ids = set(
            db.execute(select(Paper.id).where(Paper.id.in_(clean_ids))).scalars()
        )
        rank = 0
        for pid in clean_ids:
            if pid not in existing_paper_ids:
                continue
            db.add(SavedSearchPaper(saved_search_id=existing.id, paper_id=pid, rank=rank))
            rank += 1
    return existing.id


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/papers/search", response_model=schemas.PaperSearchResponse)
def search_papers(payload: schemas.PaperSearchRequest, db: Session = Depends(get_db)):
    if payload.search_mode == "paper_name":
        query_text = (payload.paper_name or "").strip()
    else:
        query_text = (payload.topic or "").strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")

    papers: list[Paper] = []
    source = "fresh"
    search_key = build_search_key(
        search_mode=payload.search_mode,
        query_text=query_text,
        conferences=payload.conferences,
        start_year=payload.start_year,
        end_year=payload.end_year,
        max_results=payload.max_results,
    )

    if payload.use_saved_search:
        saved_papers = load_saved_search_papers(db, payload.user_id, search_key)
        if saved_papers:
            papers = saved_papers[: payload.max_results]
            source = "saved_search"

    if not papers and payload.search_mode == "topic":
        # Topic mode defaults to direct conference scraping.
        conferences = payload.conferences or list(CONFERENCE_ALIASES.keys())
        scraped = scrape_conference_websites(
            query=query_text,
            conferences=conferences,
            start_year=payload.start_year,
            end_year=payload.end_year,
            search_mode=payload.search_mode,
            max_results=payload.max_results,
        )
        for item in scraped:
            papers.append(upsert_paper(db, item))
            if len(papers) >= payload.max_results:
                break

        # Local cache fallback only (no external scholar API for topic mode).
        if not papers:
            clauses = [
                or_(
                    Paper.title.ilike(f"%{query_text}%"),
                    Paper.abstract.ilike(f"%{query_text}%"),
                )
            ]
            if payload.start_year:
                clauses.append(Paper.year >= payload.start_year)
            if payload.end_year:
                clauses.append(Paper.year <= payload.end_year)
            if payload.conferences:
                venue_filters = [Paper.venue.ilike(f"%{conf}%") for conf in payload.conferences]
                query = select(Paper).where(and_(*clauses), or_(*venue_filters))
            else:
                query = select(Paper).where(and_(*clauses))
            papers = list(db.execute(query.limit(payload.max_results)).scalars().all())
    elif not papers:
        # Paper-name mode uses scholarly APIs.
        scholar = ScholarClient()
        raw_results: list[dict] = []
        try:
            candidate_limit = min(300, max(120, payload.max_results * 2))
            raw_results = scholar.search_papers(
                query_text,
                limit=candidate_limit,
                conferences=payload.conferences,
                start_year=payload.start_year,
                end_year=payload.end_year,
            )
        except Exception:
            raw_results = []

        has_year_filter = payload.start_year is not None or payload.end_year is not None
        for raw in raw_results:
            normalized = normalize_paper(raw)
            if not normalized.get("title"):
                continue
            title_l = (normalized.get("title") or "").lower()
            if query_text.lower() not in title_l:
                continue
            if has_year_filter:
                year = normalized.get("year")
                if year is None:
                    continue
                if payload.start_year and year < payload.start_year:
                    continue
                if payload.end_year and year > payload.end_year:
                    continue
            if payload.conferences and not matches_conference(normalized.get("venue"), payload.conferences):
                continue
            papers.append(upsert_paper(db, normalized))
            if len(papers) >= payload.max_results:
                break

        # If API misses, scrape official conference pages as fallback.
        if not papers:
            conferences = payload.conferences or list(CONFERENCE_ALIASES.keys())
            scraped = scrape_conference_websites(
                query=query_text,
                conferences=conferences,
                start_year=payload.start_year,
                end_year=payload.end_year,
                search_mode=payload.search_mode,
                max_results=payload.max_results,
            )
            for item in scraped:
                papers.append(upsert_paper(db, item))
                if len(papers) >= payload.max_results:
                    break

        if not papers:
            clauses = [Paper.title.ilike(f"%{query_text}%")]
            if payload.start_year:
                clauses.append(Paper.year >= payload.start_year)
            if payload.end_year:
                clauses.append(Paper.year <= payload.end_year)
            if payload.conferences:
                venue_filters = [Paper.venue.ilike(f"%{conf}%") for conf in payload.conferences]
                query = select(Paper).where(and_(*clauses), or_(*venue_filters))
            else:
                query = select(Paper).where(and_(*clauses))
            papers = list(db.execute(query.limit(payload.max_results)).scalars().all())

    papers = dedupe_papers(papers)
    if source == "fresh":
        # Conference scraping often lacks citation counts; enrich so ranking is meaningful.
        enrich_missing_citations(db, papers, max_lookups=min(120, max(20, payload.max_results // 2)))
    # Rank search results by citation count descending.
    papers.sort(key=lambda p: ((p.citation_count or 0), (p.review_score_avg or -1.0)), reverse=True)

    # Auto-cache fresh search results so pagination does not trigger re-scraping/re-querying.
    if payload.use_saved_search and source == "fresh":
        save_search_snapshot(
            db=db,
            user_id=payload.user_id,
            search_key=search_key,
            search_mode=payload.search_mode,
            query_text=query_text,
            conferences=payload.conferences,
            start_year=payload.start_year,
            end_year=payload.end_year,
            max_results=payload.max_results,
            paper_ids=[p.id for p in papers],
        )

    db.commit()
    page = payload.page
    page_size = payload.page_size
    total = len(papers)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    end = start + page_size
    paged_papers = papers[start:end]

    paper_ids = [p.id for p in paged_papers]
    favorites = set(
        db.execute(
            select(Favorite.paper_id).where(
                Favorite.user_id == payload.user_id, Favorite.paper_id.in_(paper_ids)
            )
        ).scalars()
    )

    response_papers = [paper_to_output(p, is_favorited=p.id in favorites) for p in paged_papers]
    return {
        "papers": response_papers,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "source": source,
        "all_paper_ids": [p.id for p in papers],
    }


@app.get("/api/papers/{paper_id}", response_model=schemas.PaperDetailResponse)
def get_paper_detail(
    paper_id: str,
    user_id: str = Query(default="demo-user"),
    db: Session = Depends(get_db),
):
    paper = db.execute(select(Paper).where(Paper.id == paper_id)).scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found.")

    references_preview: list[dict] = []
    reference_candidates: list[dict] = []
    references_count = 0
    external_lookup_id = paper.external_id
    detail_cache = db.execute(
        select(PaperDetailCache).where(PaperDetailCache.paper_id == paper.id)
    ).scalar_one_or_none()
    if detail_cache:
        references_count = detail_cache.references_count or 0
        cached_preview = load_json_list(detail_cache.references_preview_json)
        references_preview = [x for x in cached_preview if isinstance(x, dict)][:10]

    # Enrich scraped entries with canonical metadata before analysis/ref traversal.
    if (not paper.abstract or not paper.external_id or str(paper.external_id).startswith("SCRAPE:")) and not detail_cache:
        scholar = ScholarClient()
        try:
            candidates = scholar.search_papers(query=paper.title, limit=8)
            best = None
            best_score = 0.0
            for raw in candidates:
                normalized = normalize_paper(raw)
                if not normalized.get("title"):
                    continue
                sim = _title_similarity(paper.title, normalized.get("title"))
                year_bonus = 0.0
                if paper.year and normalized.get("year"):
                    try:
                        if abs(int(paper.year) - int(normalized["year"])) <= 1:
                            year_bonus = 0.08
                    except Exception:
                        year_bonus = 0.0
                score = sim + year_bonus
                if score > best_score:
                    best_score = score
                    best = normalized
            if best and best_score >= 0.78:
                changed = False
                if best.get("abstract") and not paper.abstract:
                    paper.abstract = best.get("abstract")
                    changed = True
                if best.get("url") and not paper.url:
                    paper.url = best.get("url")
                    changed = True
                if best.get("citation_count"):
                    paper.citation_count = max(paper.citation_count or 0, best.get("citation_count") or 0)
                    changed = True
                if best.get("venue") and not paper.venue:
                    paper.venue = best.get("venue")
                    changed = True
                best_external = best.get("external_id")
                if best_external:
                    external_lookup_id = best_external
                    # Preserve unique constraint: only set on row when not owned by another paper.
                    conflict = db.execute(
                        select(Paper).where(Paper.external_id == best_external, Paper.id != paper.id)
                    ).scalar_one_or_none()
                    if conflict is None and paper.external_id != best_external:
                        paper.external_id = best_external
                        changed = True
                if changed:
                    db.commit()
                    db.refresh(paper)
                    external_lookup_id = paper.external_id or external_lookup_id
        except Exception:
            pass

    # Only fetch external references once; subsequent detail opens reuse cached detail data.
    if external_lookup_id and not detail_cache:
        scholar = ScholarClient()
        try:
            payload = scholar.get_paper(external_lookup_id)
            if payload:
                normalized = normalize_paper(payload)
                paper = upsert_paper(db, normalized)
                refs = normalized.get("references", [])
                references_count = len(refs)
                for raw in refs[:60]:
                    normalized_ref = normalize_paper(raw)
                    if not normalized_ref.get("title"):
                        continue
                    ref_paper = upsert_paper(db, normalized_ref)
                    candidate = {
                        "id": ref_paper.id,
                        "title": ref_paper.title,
                        "year": ref_paper.year,
                        "venue": ref_paper.venue,
                        "citation_count": ref_paper.citation_count,
                        "url": ref_paper.url,
                        "abstract": ref_paper.abstract,
                    }
                    reference_candidates.append(candidate)
                    if len(references_preview) >= 10:
                        continue
                    references_preview.append(
                        {
                            "id": ref_paper.id,
                            "title": ref_paper.title,
                            "year": ref_paper.year,
                            "venue": ref_paper.venue,
                            "citation_count": ref_paper.citation_count,
                        }
                    )
                detail_cache = PaperDetailCache(
                    paper_id=paper.id,
                    references_count=references_count,
                    references_preview_json=json.dumps(references_preview),
                )
                db.add(detail_cache)
                db.commit()
        except Exception:
            references_preview = []
            references_count = 0

    is_favorited = (
        db.execute(
            select(Favorite).where(Favorite.user_id == user_id, Favorite.paper_id == paper.id)
        ).scalar_one_or_none()
        is not None
    )
    analysis = get_or_create_paper_analysis(db, paper, reference_candidates)

    return {
        "id": paper.id,
        "external_id": paper.external_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "authors": load_authors(paper.authors_json),
        "venue": paper.venue,
        "year": paper.year,
        "citation_count": paper.citation_count,
        "review_score_avg": paper.review_score_avg,
        "review_count": paper.review_count or 0,
        "decision": paper.decision,
        "url": paper.url,
        "is_favorited": is_favorited,
        "references_count": references_count,
        "references_preview": references_preview,
        "quick_takeaways": analysis.get("quick_takeaways", []),
        "logic_summary": analysis.get("logic_summary", ""),
        "evidence_points": analysis.get("evidence_points", []),
        "limitations": analysis.get("limitations", []),
        "analysis_model": analysis.get("model_name"),
        "key_dependencies": analysis.get("key_dependencies", []),
        "dataset_dependencies": analysis.get("dataset_dependencies", []),
    }


@app.post("/api/searches/save", response_model=schemas.SaveSearchResponse)
def save_search(payload: schemas.SaveSearchRequest, db: Session = Depends(get_db)):
    if payload.search_mode == "paper_name":
        query_text = (payload.paper_name or "").strip()
    else:
        query_text = (payload.topic or "").strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")
    if not payload.paper_ids:
        raise HTTPException(status_code=400, detail="No paper IDs provided to save.")

    search_key = build_search_key(
        search_mode=payload.search_mode,
        query_text=query_text,
        conferences=payload.conferences,
        start_year=payload.start_year,
        end_year=payload.end_year,
        max_results=payload.max_results,
    )
    save_search_snapshot(
        db=db,
        user_id=payload.user_id,
        search_key=search_key,
        search_mode=payload.search_mode,
        query_text=query_text,
        conferences=payload.conferences,
        start_year=payload.start_year,
        end_year=payload.end_year,
        max_results=payload.max_results,
        paper_ids=payload.paper_ids,
    )
    db.commit()
    return {"success": True, "saved_count": len(payload.paper_ids)}


@app.post("/api/favorites", response_model=schemas.FavoriteResponse)
def save_favorite(payload: schemas.FavoriteRequest, db: Session = Depends(get_db)):
    paper = db.execute(select(Paper).where(Paper.id == payload.paper_id)).scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found.")

    existing = db.execute(
        select(Favorite).where(
            Favorite.user_id == payload.user_id,
            Favorite.paper_id == payload.paper_id,
        )
    ).scalar_one_or_none()
    if not existing:
        db.add(Favorite(user_id=payload.user_id, paper_id=payload.paper_id))
        db.commit()

    return {"success": True}


@app.delete("/api/favorites/{paper_id}", response_model=schemas.FavoriteResponse)
def remove_favorite(
    paper_id: str,
    user_id: str = Query(default="demo-user"),
    db: Session = Depends(get_db),
):
    db.execute(delete(Favorite).where(Favorite.user_id == user_id, Favorite.paper_id == paper_id))
    db.commit()
    return {"success": True}


@app.get("/api/favorites", response_model=schemas.PaperSearchResponse)
def list_favorites(user_id: str = Query(default="demo-user"), db: Session = Depends(get_db)):
    rows = db.execute(
        select(Paper)
        .join(Favorite, Favorite.paper_id == Paper.id)
        .where(Favorite.user_id == user_id)
        .order_by(Favorite.created_at.desc())
    ).scalars()
    papers = list(rows)
    serialized = [paper_to_output(p, is_favorited=True) for p in papers]
    total = len(serialized)
    return {
        "papers": serialized,
        "total": total,
        "page": 1,
        "page_size": total if total > 0 else 1,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
    }


@app.post("/api/traces", response_model=schemas.TraceStartResponse)
def start_trace(
    payload: schemas.TraceStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    paper = db.execute(select(Paper).where(Paper.id == payload.paper_id)).scalar_one_or_none()
    if not paper:
        raise HTTPException(status_code=404, detail="Paper not found.")

    stale_reason_patterns = (
        "Inferred from title similarity and citation influence when explicit references were unavailable.",
        "Likely direct technical dependency",
        "Likely foundational method paper",
        "title signals",
    )

    # Reuse cached trace map for identical request to avoid repetitive LLM/API work.
    existing = db.execute(
        select(TraceRequest)
        .where(
            TraceRequest.user_id == payload.user_id,
            TraceRequest.root_paper_id == payload.paper_id,
            TraceRequest.trace_depth == payload.trace_depth,
            TraceRequest.status.in_(["pending", "running", "completed"]),
        )
        .order_by(TraceRequest.created_at.desc())
    ).scalars().first()
    if existing and existing.status in {"pending", "running"}:
        return {"trace_id": existing.id, "status": existing.status}
    if existing and existing.status == "completed":
        should_rerun = False

        analysis = db.execute(
            select(PaperAnalysis).where(PaperAnalysis.paper_id == payload.paper_id)
        ).scalar_one_or_none()
        if analysis and analysis.updated_at:
            trace_finished_at = existing.completed_at or existing.created_at
            if trace_finished_at and analysis.updated_at > trace_finished_at:
                should_rerun = True

        node_count = db.execute(
            select(func.count(TraceGraphNode.id)).where(TraceGraphNode.trace_request_id == existing.id)
        ).scalar_one()
        edge_count = db.execute(
            select(func.count(TraceGraphEdge.id)).where(TraceGraphEdge.trace_request_id == existing.id)
        ).scalar_one()
        if node_count <= 1 or edge_count == 0:
            should_rerun = True

        stale_reason_count = db.execute(
            select(func.count(TraceGraphEdge.id)).where(
                TraceGraphEdge.trace_request_id == existing.id,
                or_(
                    *[TraceGraphEdge.reason.contains(pattern) for pattern in stale_reason_patterns]
                ),
            )
        ).scalar_one()
        if stale_reason_count > 0:
            should_rerun = True

        if not should_rerun:
            return {"trace_id": existing.id, "status": existing.status}

    trace_req = TraceRequest(
        user_id=payload.user_id,
        root_paper_id=payload.paper_id,
        trace_depth=payload.trace_depth,
        max_branching=3,
        status="pending",
    )
    db.add(trace_req)
    db.commit()
    db.refresh(trace_req)

    background_tasks.add_task(run_trace_job, trace_req.id)
    return {"trace_id": trace_req.id, "status": trace_req.status}


@app.get("/api/traces/{trace_id}", response_model=schemas.TraceStatusResponse)
def get_trace(trace_id: int, db: Session = Depends(get_db)):
    trace_req = db.execute(select(TraceRequest).where(TraceRequest.id == trace_id)).scalar_one_or_none()
    if not trace_req:
        raise HTTPException(status_code=404, detail="Trace request not found.")

    nodes_out: list[dict] = []
    edges_out: list[dict] = []

    if trace_req.status in {"running", "completed"}:
        node_rows = db.execute(
            select(TraceGraphNode, Paper)
            .join(Paper, Paper.id == TraceGraphNode.paper_id)
            .where(TraceGraphNode.trace_request_id == trace_id)
            .order_by(TraceGraphNode.level.asc(), Paper.citation_count.desc())
        ).all()
        for node, paper in node_rows:
            nodes_out.append(
                {
                    "paper_id": node.paper_id,
                    "level": node.level,
                    "title": paper.title,
                    "venue": paper.venue,
                    "year": paper.year,
                    "citation_count": paper.citation_count,
                }
            )
        paper_lookup = {paper.id: paper for _, paper in node_rows}

        edge_rows = list(
            db.execute(
                select(TraceGraphEdge).where(TraceGraphEdge.trace_request_id == trace_id)
            ).scalars()
        )
        updated_edge_reason = False
        stale_reason_patterns = (
            "Likely direct technical dependency",
            "Likely foundational method paper",
            "Inferred from title similarity and citation influence when explicit references were unavailable.",
            "High-citation method-related reference based on title signals.",
        )
        for edge_obj in edge_rows:
            reason = edge_obj.reason or ""
            if not isinstance(reason, str):
                reason = ""
            should_refresh = not reason or any(p in reason for p in stale_reason_patterns)
            if should_refresh:
                source_paper = paper_lookup.get(edge_obj.source_paper_id)
                target_paper = paper_lookup.get(edge_obj.target_paper_id)
                if source_paper and target_paper:
                    refreshed = explain_trace_edge(
                        source_paper={
                            "title": source_paper.title,
                            "abstract": source_paper.abstract,
                            "venue": source_paper.venue,
                            "year": source_paper.year,
                        },
                        target_paper={
                            "title": target_paper.title,
                            "abstract": target_paper.abstract,
                            "venue": target_paper.venue,
                            "year": target_paper.year,
                        },
                        relation_type=edge_obj.relation_type,
                        base_reason=reason,
                    )
                    if refreshed and refreshed != reason:
                        edge_obj.reason = refreshed
                        reason = refreshed
                        updated_edge_reason = True

            edges_out.append(
                {
                    "source_paper_id": edge_obj.source_paper_id,
                    "target_paper_id": edge_obj.target_paper_id,
                    "relation_type": edge_obj.relation_type,
                    "confidence": edge_obj.confidence,
                    "reason": reason,
                }
            )
        if updated_edge_reason:
            db.commit()

    return {
        "trace_id": trace_req.id,
        "status": trace_req.status,
        "trace_depth": trace_req.trace_depth,
        "created_at": trace_req.created_at,
        "completed_at": trace_req.completed_at,
        "error_message": trace_req.error_message,
        "root_paper_id": trace_req.root_paper_id,
        "nodes": nodes_out,
        "edges": edges_out,
    }
