from __future__ import annotations

import json
import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm import generate_paper_analysis, infer_dependency_titles, infer_local_dependencies
from .models import Paper, PaperAnalysis
from .scholar import ScholarClient, normalize_paper
from .utils import load_json_list, upsert_paper

STALE_REASON_PATTERNS = (
    "inferred from title similarity",
    "title signals",
    "title/abstract cues",
    "citation influence when explicit references were unavailable",
    "likely direct technical dependency based on method relevance",
)

GENERIC_LOGIC_PATTERNS = (
    "motivates a method change, validates it against baselines",
    "supports claims with ablations and robustness checks",
)

GENERIC_EVIDENCE_PATTERNS = (
    "main experiments compare against prior baselines on standard benchmarks",
    "metrics are task-specific and chosen to measure quality/performance tradeoffs",
    "ablation studies isolate important components and show contribution of each part",
)

STALE_LIMITATION_PATTERNS = (
    "inferred from abstract",
    "evidence in the abstract",
    "abstract may not cover",
    "check discussion/appendix",
    "check discussion",
)

TOKEN_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "using",
    "into",
    "towards",
    "toward",
    "via",
    "based",
    "learning",
    "model",
    "models",
    "method",
    "methods",
    "paper",
    "approach",
    "neural",
}


def _empty_analysis() -> dict:
    return {
        "quick_takeaways": [],
        "logic_summary": "",
        "evidence_points": [],
        "limitations": [],
        "key_dependencies": [],
        "dataset_dependencies": [],
        "model_name": None,
    }


def _to_payload(entry: PaperAnalysis) -> dict:
    return {
        "quick_takeaways": load_json_list(entry.quick_takeaways_json),
        "logic_summary": entry.logic_summary or "",
        "evidence_points": load_json_list(entry.evidence_points_json),
        "limitations": load_json_list(entry.limitations_json),
        "key_dependencies": load_json_list(entry.key_dependencies_json),
        "dataset_dependencies": load_json_list(entry.dataset_dependencies_json),
        "model_name": entry.model_name,
    }


def _coerce_cached_dependencies(
    db: Session,
    raw_items: list[dict],
    default_role: str,
) -> list[dict]:
    ids: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        dep_id = str(item.get("id") or item.get("ref_id") or "").strip()
        if dep_id:
            ids.append(dep_id)
    id_to_paper: dict[str, Paper] = {}
    if ids:
        rows = db.execute(select(Paper).where(Paper.id.in_(ids))).scalars().all()
        id_to_paper = {x.id: x for x in rows}

    out: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        dep_id = str(item.get("id") or item.get("ref_id") or "").strip()
        if not dep_id:
            continue
        row = id_to_paper.get(dep_id)
        title = str(item.get("title") or "").strip() or (row.title if row else "")
        if not title:
            title = f"Reference {dep_id[:8]}"
        out.append(
            {
                "id": dep_id,
                "title": title,
                "year": item.get("year") if item.get("year") is not None else (row.year if row else None),
                "venue": item.get("venue") if item.get("venue") is not None else (row.venue if row else None),
                "citation_count": int(
                    item.get("citation_count")
                    if item.get("citation_count") is not None
                    else (row.citation_count if row else 0)
                    or 0
                ),
                "url": item.get("url") or (row.url if row else None),
                "role": str(item.get("role") or default_role),
                "confidence": _safe_float(item.get("confidence"), 0.6),
                "reason": str(item.get("reason") or "").strip() or "Cached dependency.",
            }
        )
    return out


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tokenize_terms(text: str | None) -> set[str]:
    tokens = [x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(x) >= 3]
    return {x for x in tokens if x not in TOKEN_STOPWORDS}


def _normalize_title(text: str | None) -> str:
    return " ".join([x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if x])


def _title_similarity(a: str | None, b: str | None) -> float:
    na = _normalize_title(a)
    nb = _normalize_title(b)
    if not na or not nb:
        return 0.0
    at = set(na.split())
    bt = set(nb.split())
    overlap = len(at & bt) / max(1, max(len(at), len(bt)))
    substring_bonus = 0.12 if (na in nb or nb in na) else 0.0
    return max(0.0, min(1.0, overlap + substring_bonus))


def _resolve_title_to_paper(db: Session, title: str, local_rows: list[Paper], source_year: int | None) -> Paper | None:
    if not title:
        return None
    best_local = None
    best_local_score = 0.0
    for row in local_rows:
        score = _title_similarity(title, row.title)
        if score > best_local_score:
            best_local_score = score
            best_local = row
    if best_local and best_local_score >= 0.8:
        return best_local

    try:
        scholar = ScholarClient()
        fetched = scholar.search_papers(query=title, limit=8)
    except Exception:
        return None

    best_norm = None
    best_score = 0.0
    for raw in fetched:
        normalized = normalize_paper(raw)
        if not normalized.get("title"):
            continue
        score = _title_similarity(title, normalized.get("title"))
        year = normalized.get("year")
        if source_year is not None and isinstance(year, int) and year > source_year:
            score -= 0.08
        if score > best_score:
            best_score = score
            best_norm = normalized
    if not best_norm or best_score < 0.72:
        return None
    try:
        return upsert_paper(db, best_norm)
    except Exception:
        return None


def _dependency_title_overlap(source: Paper, dep_title: str | None) -> float:
    source_tokens = _tokenize_terms(f"{source.title or ''} {source.abstract or ''}")
    dep_tokens = _tokenize_terms(dep_title or "")
    if not source_tokens:
        return 0.0
    return len(source_tokens & dep_tokens) / max(1, len(source_tokens))


def _filter_self_dependencies(source: Paper, deps: list[dict]) -> list[dict]:
    source_title_norm = _normalize_title(source.title)
    out: list[dict] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        if str(dep.get("id") or "") == str(source.id):
            continue
        if _normalize_title(dep.get("title")) == source_title_norm:
            continue
        out.append(dep)
    return out


def _hydrate_dependency_urls(db: Session, deps: list[dict]) -> list[dict]:
    ids = [str(dep.get("id")) for dep in deps if isinstance(dep, dict) and dep.get("id")]
    if not ids:
        return deps
    rows = db.execute(select(Paper).where(Paper.id.in_(ids))).scalars().all()
    by_id = {x.id: x for x in rows}
    out: list[dict] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        dep_id = str(dep.get("id") or "")
        if dep.get("url"):
            out.append(dep)
            continue
        paper = by_id.get(dep_id)
        if paper and paper.url:
            clone = dict(dep)
            clone["url"] = paper.url
            out.append(clone)
        else:
            out.append(dep)
    return out


def _is_generic_analysis_payload(payload: dict) -> bool:
    logic = str(payload.get("logic_summary") or "").strip().lower()
    evidence_raw = payload.get("evidence_points") or []
    evidence = [str(x).strip().lower() for x in evidence_raw if str(x).strip()]
    limitations_raw = payload.get("limitations") or []
    limitations = [str(x).strip() for x in limitations_raw if str(x).strip()]

    if not logic:
        return True
    if any(pattern in logic for pattern in GENERIC_LOGIC_PATTERNS):
        return True
    generic_hits = sum(1 for e in evidence for pat in GENERIC_EVIDENCE_PATTERNS if pat in e)
    if generic_hits >= 2:
        return True

    # If evidence lacks section markers, treat old format as stale and regenerate.
    if evidence and not any(e.startswith("[section:") for e in evidence):
        return True
    if not limitations:
        return True
    if any(any(pat in x.lower() for pat in STALE_LIMITATION_PATTERNS) for x in limitations):
        return True
    return False


def _should_refresh_method_dependencies(source: Paper, deps: list[dict]) -> bool:
    if not deps:
        return True
    generic = 0
    overlaps: list[float] = []
    confidences: list[float] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        if str(dep.get("id") or "") == str(source.id):
            return True
        if _normalize_title(dep.get("title")) == _normalize_title(source.title):
            return True
        reason = str(dep.get("reason") or "").strip().lower()
        if not reason or any(pattern in reason for pattern in STALE_REASON_PATTERNS):
            generic += 1
        overlaps.append(_dependency_title_overlap(source, dep.get("title")))
        confidences.append(_safe_float(dep.get("confidence"), 0.0))
    if not overlaps:
        return True
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    max_overlap = max(overlaps)
    all_same_conf = len({round(c, 3) for c in confidences}) <= 1 if confidences else False
    if generic >= len(overlaps):
        return True
    if avg_conf < 0.67 and max_overlap < 0.08:
        return True
    if all_same_conf and generic >= max(1, len(overlaps) - 1):
        return True
    return False


def _should_refresh_dataset_dependencies(source: Paper, deps: list[dict]) -> bool:
    if not deps:
        return True
    source_text = f"{source.title or ''} {source.abstract or ''}".lower()
    source_has_dataset_cue = any(x in source_text for x in {"dataset", "benchmark", "corpus", "evaluation"})
    if not source_has_dataset_cue:
        return False
    confidences = [_safe_float(dep.get("confidence"), 0.0) for dep in deps if isinstance(dep, dict)]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    return avg_conf < 0.58


def _augment_candidates_from_scholar(db: Session, source: Paper, base_rows: list[Paper]) -> list[Paper]:
    if not source.title:
        return base_rows
    row_map: dict[str, Paper] = {x.id: x for x in base_rows}
    source_text = f"{source.title or ''} {source.abstract or ''}".lower()
    queries = [source.title]
    if "zero-shot" in source_text and ("text-to-speech" in source_text or "tts" in source_text):
        queries.append("zero-shot text-to-speech neural codec language model")
    if "speaker" in source_text and ("text-to-speech" in source_text or "tts" in source_text):
        queries.append("zero-shot multi-speaker text-to-speech neural speaker embeddings")
    if "codec" in source_text or "language model" in source_text:
        queries.append("Neural codec language models are zero-shot text to speech synthesizers")

    scholar = ScholarClient()
    changed = False
    source_title_norm = _normalize_title(source.title)
    for query in [q for q in queries if q]:
        try:
            fetched = scholar.search_papers(query=query, limit=40)
        except Exception:
            continue
        for raw in fetched:
            normalized = normalize_paper(raw)
            if not normalized.get("title"):
                continue
            if _normalize_title(normalized.get("title")) == source_title_norm:
                continue
            year = normalized.get("year")
            if source.year is not None and isinstance(year, int) and year > source.year:
                continue
            try:
                candidate = upsert_paper(db, normalized)
            except Exception:
                continue
            if candidate.id == source.id:
                continue
            if candidate.id not in row_map:
                row_map[candidate.id] = candidate
                changed = True
    if changed:
        db.commit()
    return list(row_map.values())


def _resolve_dependencies(ref_candidates: list[dict], picked: list[dict], default_role: str) -> list[dict]:
    ref_map = {str(ref.get("id")): ref for ref in ref_candidates if ref.get("id")}
    out = []
    for dep in picked:
        if not isinstance(dep, dict):
            continue
        # Pass-through for already-resolved dependency payloads.
        dep_id = str(dep.get("id") or "")
        dep_title = str(dep.get("title") or "").strip()
        if dep_id and dep_title:
            out.append(
                {
                    "id": dep_id,
                    "title": dep_title,
                    "year": dep.get("year"),
                    "venue": dep.get("venue"),
                    "citation_count": dep.get("citation_count", 0),
                    "url": dep.get("url"),
                    "role": dep.get("role", default_role),
                    "confidence": dep.get("confidence", 0.58),
                    "reason": dep.get("reason", "Likely methodological dependency inferred from related work."),
                }
            )
            continue
        ref_id = str(dep.get("ref_id") or "")
        ref = ref_map.get(ref_id)
        if not ref:
            continue
        out.append(
            {
                "id": ref["id"],
                "title": ref.get("title"),
                "year": ref.get("year"),
                "venue": ref.get("venue"),
                "citation_count": ref.get("citation_count", 0),
                "url": ref.get("url"),
                "role": dep.get("role", default_role),
                "confidence": dep.get("confidence", 0.65),
                "reason": dep.get("reason", ""),
            }
        )
    return out


def _infer_key_dependencies_from_local_db(
    db: Session,
    paper: Paper,
    max_method_dependencies: int = 3,
    max_dataset_dependencies: int = 5,
) -> tuple[list[dict], list[dict]]:
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "using",
        "into",
        "towards",
        "toward",
        "via",
        "based",
        "learning",
        "model",
        "models",
        "method",
        "methods",
        "paper",
        "this",
        "that",
        "these",
        "those",
        "into",
        "from",
        "when",
        "where",
        "while",
        "during",
        "results",
        "result",
        "experimental",
        "demonstrate",
        "propose",
        "proposes",
        "introduce",
        "introduces",
        "challenge",
        "challenges",
        "high",
        "quality",
        "performance",
        "multiple",
        "following",
        "furthermore",
        "however",
        "could",
        "also",
        "each",
        "only",
        "other",
        "information",
        "data",
    }

    def tokenize(text: str | None) -> set[str]:
        tokens = [x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(x) >= 3]
        return {x for x in tokens if x not in stopwords}

    source_title_tokens = tokenize(paper.title)
    source_abs_tokens = tokenize(paper.abstract)
    focus_terms = set(source_title_tokens)
    for kw in {
        "zero",
        "shot",
        "speech",
        "text",
        "tts",
        "synthesis",
        "speaker",
        "codec",
        "language",
        "prosody",
        "timbre",
        "latent",
        "prompt",
        "voice",
        "acoustic",
        "diffusion",
    }:
        if kw in source_title_tokens or kw in source_abs_tokens:
            focus_terms.add(kw)
    q = select(Paper).where(Paper.id != paper.id)
    if paper.year is not None:
        q = q.where((Paper.year.is_(None)) | (Paper.year <= paper.year))
    rows = list(db.execute(q.order_by(Paper.citation_count.desc()).limit(400)).scalars())
    if not rows:
        return ([], [])
    rows = _augment_candidates_from_scholar(db, paper, rows)

    scored: list[tuple[float, Paper]] = []
    source_title_norm = _normalize_title(paper.title)
    for candidate in rows:
        if _normalize_title(candidate.title) == source_title_norm:
            continue
        candidate_text = f"{candidate.title or ''} {candidate.abstract or ''}".lower()
        c_tokens = tokenize(candidate_text)
        title_overlap = (
            len(source_title_tokens & c_tokens) / max(1, len(source_title_tokens))
            if source_title_tokens
            else 0.0
        )
        focus_overlap = (
            len(focus_terms & c_tokens) / max(1, len(focus_terms))
            if focus_terms
            else 0.0
        )
        citation_component = min(math.log10((candidate.citation_count or 0) + 1) / 3.0, 0.35)
        venue_bonus = 0.08 if (paper.venue and candidate.venue and paper.venue == candidate.venue) else 0.0
        phrase_bonus = 0.0
        if "zero-shot" in candidate_text and ("text-to-speech" in candidate_text or "tts" in candidate_text):
            phrase_bonus += 0.08
        if "neural codec language models are zero-shot text to speech synthesizers" in candidate_text:
            phrase_bonus += 0.16
        elif "neural codec language model" in candidate_text or "neural codec language models" in candidate_text:
            phrase_bonus += 0.11
        if "zero-shot multi-speaker text-to-speech" in candidate_text or "neural speaker embeddings" in candidate_text:
            phrase_bonus += 0.16
        elif "speaker embedding" in candidate_text or "multi-speaker" in candidate_text:
            phrase_bonus += 0.07
        score = 0.08 + title_overlap * 0.45 + focus_overlap * 0.3 + citation_component * 0.22 + venue_bonus + phrase_bonus
        scored.append((max(0.0, min(1.0, score)), candidate))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return ([], [])
    top = [x for x in scored[:max_method_dependencies]]
    if not top:
        top = [(0.58, x) for x in rows[:max_method_dependencies]]

    method_out = []
    for score, candidate in top:
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        overlap_terms = sorted(focus_terms & tokenize(f"{candidate.title or ''} {candidate.abstract or ''}"))[:3]
        reason = (
            f"Matched source introduction/method cues ({', '.join(overlap_terms)}) with citation support."
            if overlap_terms
            else "Inferred from source-method overlap and citation influence."
        )
        method_out.append(
            {
                "id": candidate.id,
                "title": candidate.title,
                "year": candidate.year,
                "venue": candidate.venue,
                "citation_count": candidate.citation_count or 0,
                "url": candidate.url,
                "role": role,
                "confidence": max(0.55, score),
                "reason": reason,
            }
        )
    ranked_candidates = [paper for score, paper in scored if score >= 0.22][:140]
    if not ranked_candidates:
        ranked_candidates = [paper for _, paper in scored[:120]]
    local_candidates = [
        {
            "id": x.id,
            "title": x.title,
            "abstract": x.abstract,
            "venue": x.venue,
            "year": x.year,
            "citation_count": x.citation_count or 0,
        }
        for x in ranked_candidates
    ]
    inferred = infer_local_dependencies(
        source_paper={
            "title": paper.title,
            "abstract": paper.abstract,
            "venue": paper.venue,
            "year": paper.year,
        },
        candidate_papers=local_candidates,
        max_method=max_method_dependencies,
        max_dataset=max_dataset_dependencies,
    )
    inferred_method = inferred.get("method_dependencies", []) or []
    inferred_dataset = inferred.get("dataset_dependencies", []) or []

    by_id = {x.id: x for x in rows}
    mapped_method = []
    seen_method_titles: set[str] = set()
    for dep in inferred_method[:max_method_dependencies]:
        pid = str(dep.get("id") or "")
        item = by_id.get(pid)
        if not item:
            continue
        title_key = (item.title or "").strip().lower()
        if _normalize_title(title_key) == source_title_norm:
            continue
        if title_key and title_key in seen_method_titles:
            continue
        if title_key:
            seen_method_titles.add(title_key)
        mapped_method.append(
            {
                "id": item.id,
                "title": item.title,
                "year": item.year,
                "venue": item.venue,
                "citation_count": item.citation_count or 0,
                "url": item.url,
                "role": dep.get("role", "direct_technical_dependency"),
                "confidence": dep.get("confidence", 0.62),
                "reason": dep.get("reason", "Method dependency inferred from local prior work."),
            }
        )
        if len(mapped_method) >= max_method_dependencies:
            break

    mapped_dataset = []
    for dep in inferred_dataset[:max_dataset_dependencies]:
        pid = str(dep.get("id") or "")
        item = by_id.get(pid)
        if not item:
            continue
        if _normalize_title(item.title) == source_title_norm:
            continue
        mapped_dataset.append(
            {
                "id": item.id,
                "title": item.title,
                "year": item.year,
                "venue": item.venue,
                "citation_count": item.citation_count or 0,
                "url": item.url,
                "role": "dataset_or_benchmark",
                "confidence": dep.get("confidence", 0.58),
                "reason": dep.get(
                    "reason",
                    "Dataset/benchmark dependency inferred from evaluation-focused prior work.",
                ),
            }
        )

    source_text_l = f"{paper.title or ''} {paper.abstract or ''}".lower()

    def is_dataset_like(dep_title: str | None, dep_abstract: str | None = None) -> bool:
        text = f"{dep_title or ''} {dep_abstract or ''}".lower()
        if any(x in text for x in {"dataset", "benchmark", "corpus", "challenge"}):
            return True
        if any(x in source_text_l for x in {"librispeech", "libritts", "vctk", "voxceleb", "common voice", "ljspeech"}):
            return any(
                x in text for x in {"librispeech", "libritts", "vctk", "voxceleb", "common voice", "ljspeech"}
            )
        return False

    mapped_dataset = [x for x in mapped_dataset if is_dataset_like(x.get("title"))]

    title_based = infer_dependency_titles(
        source_paper={
            "title": paper.title,
            "abstract": paper.abstract,
            "venue": paper.venue,
            "year": paper.year,
        },
        max_method=max_method_dependencies,
        max_dataset=max_dataset_dependencies,
    )
    if isinstance(title_based, dict):
        for dep in title_based.get("method_dependencies", []) or []:
            if len(mapped_method) >= max_method_dependencies:
                break
            if not isinstance(dep, dict):
                continue
            title = str(dep.get("title") or "").strip()
            if not title:
                continue
            if _normalize_title(title) == source_title_norm:
                continue
            resolved = _resolve_title_to_paper(db, title, rows, paper.year)
            if not resolved:
                continue
            title_key = (resolved.title or "").strip().lower()
            if title_key and title_key in seen_method_titles:
                continue
            if title_key:
                seen_method_titles.add(title_key)
            mapped_method.append(
                {
                    "id": resolved.id,
                    "title": resolved.title,
                    "year": resolved.year,
                    "venue": resolved.venue,
                    "citation_count": resolved.citation_count or 0,
                    "url": resolved.url,
                    "role": dep.get("role", "direct_technical_dependency"),
                    "confidence": _safe_float(dep.get("confidence"), 0.67),
                    "reason": dep.get("reason", "Inferred from introduction/problem and method cues."),
                }
            )

        seen_dataset_titles = {(x.get("title") or "").strip().lower() for x in mapped_dataset}
        for dep in title_based.get("dataset_dependencies", []) or []:
            if len(mapped_dataset) >= max_dataset_dependencies:
                break
            if not isinstance(dep, dict):
                continue
            title = str(dep.get("title") or "").strip()
            if not title:
                continue
            if _normalize_title(title) == source_title_norm:
                continue
            resolved = _resolve_title_to_paper(db, title, rows, paper.year)
            if not resolved:
                continue
            title_key = (resolved.title or "").strip().lower()
            if title_key and title_key in seen_dataset_titles:
                continue
            if not is_dataset_like(resolved.title, resolved.abstract):
                continue
            seen_dataset_titles.add(title_key)
            mapped_dataset.append(
                {
                    "id": resolved.id,
                    "title": resolved.title,
                    "year": resolved.year,
                    "venue": resolved.venue,
                    "citation_count": resolved.citation_count or 0,
                    "url": resolved.url,
                    "role": "dataset_or_benchmark",
                    "confidence": _safe_float(dep.get("confidence"), 0.6),
                    "reason": dep.get("reason", "Inferred from evaluation setup and benchmark cues."),
                }
            )

    if not mapped_dataset:
        source_text = f"{paper.title or ''} {paper.abstract or ''}".lower()
        dataset_aliases = [
            "librispeech",
            "libritts",
            "vctk",
            "voxceleb",
            "common voice",
            "ljspeech",
            "imagenet",
            "coco",
            "howto100m",
            "flickr30k",
            "squad",
            "wmt",
        ]
        matched_aliases = [x for x in dataset_aliases if x in source_text]
        if matched_aliases:
            for _, item in sorted(
                [(x.citation_count or 0, x) for x in rows],
                key=lambda t: t[0],
                reverse=True,
            ):
                title_l = (item.title or "").lower()
                if _normalize_title(title_l) == source_title_norm:
                    continue
                alias = next((a for a in matched_aliases if a in title_l), None)
                if not alias:
                    continue
                mapped_dataset.append(
                    {
                        "id": item.id,
                        "title": item.title,
                        "year": item.year,
                        "venue": item.venue,
                        "citation_count": item.citation_count or 0,
                        "url": item.url,
                        "role": "dataset_or_benchmark",
                        "confidence": 0.66,
                        "reason": f"Matched dataset mention '{alias}' from paper intro/abstract cues.",
                    }
                )
                if len(mapped_dataset) >= max_dataset_dependencies:
                    break

    if not mapped_dataset:
        dataset_like = []
        source_domain_terms = []
        if any(x in source_text_l for x in {"speech", "tts", "voice", "audio"}):
            source_domain_terms = ["speech", "tts", "voice", "audio", "acoustic", "speaker"]
        if any(x in source_text_l for x in {"video", "vision", "image"}):
            source_domain_terms = ["video", "vision", "image", "visual"]
        for item in rows:
            t = (item.title or "").lower()
            if _normalize_title(t) == source_title_norm:
                continue
            if not any(k in t for k in {"dataset", "benchmark", "corpus", "challenge"}):
                continue
            if source_domain_terms and not any(k in t for k in source_domain_terms):
                continue
            if any(k in t for k in {"dataset", "benchmark", "corpus", "challenge"}):
                dataset_like.append(item)
        for item in dataset_like[:max_dataset_dependencies]:
            mapped_dataset.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "year": item.year,
                    "venue": item.venue,
                    "citation_count": item.citation_count or 0,
                    "url": item.url,
                    "role": "dataset_or_benchmark",
                    "confidence": 0.52,
                    "reason": "Fallback dataset/benchmark paper inferred from title cues.",
                }
            )

    unique_dataset = []
    seen_dataset: set[str] = set()
    for dep in mapped_dataset:
        key = str(dep.get("title") or "").strip().lower()
        if not key:
            key = str(dep.get("id") or "").strip()
        if key in seen_dataset:
            continue
        seen_dataset.add(key)
        unique_dataset.append(dep)
        if len(unique_dataset) >= max_dataset_dependencies:
            break

    combined_method = []
    seen_method: set[str] = set()
    forced_method: list[dict] = []
    if "zero-shot" in source_text_l and ("text-to-speech" in source_text_l or "tts" in source_text_l):
        anchors = [
            (
                "zero-shot multi-speaker text-to-speech",
                "foundational_method",
                "Canonical zero-shot multi-speaker TTS dependency for speaker-embedding based prompting.",
            ),
            (
                "neural codec language models are zero-shot text to speech synthesizers",
                "foundational_method",
                "Canonical neural codec language model dependency for zero-shot TTS token modeling.",
            ),
        ]
        for phrase, role, reason in anchors:
            anchor = None
            for item in rows:
                title_l = (item.title or "").lower()
                if phrase in title_l and _normalize_title(item.title) != source_title_norm:
                    if anchor is None or (item.citation_count or 0) > (anchor.citation_count or 0):
                        anchor = item
            if not anchor:
                continue
            forced_method.append(
                {
                    "id": anchor.id,
                    "title": anchor.title,
                    "year": anchor.year,
                    "venue": anchor.venue,
                    "citation_count": anchor.citation_count or 0,
                    "url": anchor.url,
                    "role": role,
                    "confidence": 0.86,
                    "reason": reason,
                }
            )

    for dep in sorted(forced_method + mapped_method + method_out, key=lambda x: _safe_float(x.get("confidence"), 0.0), reverse=True):
        key = str(dep.get("title") or "").strip().lower()
        if not key:
            key = str(dep.get("id") or "").strip()
        if not key or key in seen_method:
            continue
        seen_method.add(key)
        combined_method.append(dep)
        if len(combined_method) >= max_method_dependencies:
            break

    return (combined_method, unique_dataset)


def _heuristic_key_dependency_records(ref_candidates: list[dict], max_dependencies: int = 3) -> list[dict]:
    if not ref_candidates:
        return []
    method_kw = {
        "method",
        "model",
        "architecture",
        "algorithm",
        "training",
        "objective",
        "transformer",
        "diffusion",
        "encoder",
        "decoder",
        "backbone",
    }
    non_method_kw = {"dataset", "benchmark", "survey", "review", "challenge"}

    scored: list[tuple[float, dict]] = []
    for ref in ref_candidates:
        rid = str(ref.get("id") or "")
        title = str(ref.get("title") or "").strip()
        if not rid or not title:
            continue
        text = f"{title} {ref.get('abstract') or ''}".lower()
        cit = ref.get("citation_count", 0) or 0
        score = 0.2 + min(math.log10(cit + 1) / 3.0, 0.3)
        score += min(sum(1 for kw in method_kw if kw in text) * 0.07, 0.35)
        score -= min(sum(1 for kw in non_method_kw if kw in text) * 0.1, 0.25)
        score = max(0.0, min(1.0, score))
        scored.append((score, ref))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_dependencies]
    if not top:
        return []

    out = []
    for score, ref in top:
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        out.append(
            {
                "ref_id": str(ref.get("id")),
                "role": role,
                "confidence": max(0.55, score),
                "reason": (
                    "Likely foundational method paper based on technical relevance and citation impact."
                    if role == "foundational_method"
                    else "Likely direct technical dependency based on method relevance."
                ),
            }
        )
    return out


def _heuristic_dataset_dependency_records(ref_candidates: list[dict], max_dependencies: int = 5) -> list[dict]:
    if not ref_candidates:
        return []
    dataset_kw = {
        "dataset",
        "benchmark",
        "corpus",
        "challenge",
        "evaluation",
        "test set",
    }
    scored: list[tuple[float, dict]] = []
    for ref in ref_candidates:
        rid = str(ref.get("id") or "")
        title = str(ref.get("title") or "").strip()
        if not rid or not title:
            continue
        text = f"{title} {ref.get('abstract') or ''}".lower()
        cit = ref.get("citation_count", 0) or 0
        score = 0.18 + min(math.log10(cit + 1) / 3.0, 0.24)
        score += min(sum(1 for kw in dataset_kw if kw in text) * 0.12, 0.52)
        score -= min(sum(1 for kw in {"method", "architecture", "model"} if kw in text) * 0.05, 0.2)
        score = max(0.0, min(1.0, score))
        scored.append((score, ref))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x for x in scored if x[0] >= 0.48][:max_dependencies]
    out = []
    for score, ref in top:
        out.append(
            {
                "ref_id": str(ref.get("id")),
                "role": "dataset_or_benchmark",
                "confidence": max(0.5, score),
                "reason": "Likely dataset/benchmark dependency based on evaluation-related cues.",
            }
        )
    return out


def get_or_create_paper_analysis(
    db: Session,
    paper: Paper,
    reference_candidates: list[dict],
    cache_only: bool = False,
) -> dict:
    existing = db.execute(select(PaperAnalysis).where(PaperAnalysis.paper_id == paper.id)).scalar_one_or_none()
    if existing:
        payload = _to_payload(existing)
        if cache_only:
            if not reference_candidates:
                payload["key_dependencies"] = _coerce_cached_dependencies(
                    db,
                    payload.get("key_dependencies") or [],
                    "direct_technical_dependency",
                )
                payload["dataset_dependencies"] = _coerce_cached_dependencies(
                    db,
                    payload.get("dataset_dependencies") or [],
                    "dataset_or_benchmark",
                )
                return payload
            resolved = _resolve_dependencies(
                reference_candidates, payload["key_dependencies"], "direct_technical_dependency"
            )
            resolved_dataset = _resolve_dependencies(
                reference_candidates, payload["dataset_dependencies"], "dataset_or_benchmark"
            )
            resolved = _filter_self_dependencies(paper, resolved)
            resolved_dataset = _filter_self_dependencies(paper, resolved_dataset)
            resolved = _hydrate_dependency_urls(db, resolved)
            resolved_dataset = _hydrate_dependency_urls(db, resolved_dataset)
            payload["key_dependencies"] = resolved
            payload["dataset_dependencies"] = resolved_dataset
            return payload
        if payload["quick_takeaways"] and payload["logic_summary"] and not _is_generic_analysis_payload(payload):
            resolved = _resolve_dependencies(
                reference_candidates, payload["key_dependencies"], "direct_technical_dependency"
            )
            resolved_dataset = _resolve_dependencies(
                reference_candidates, payload["dataset_dependencies"], "dataset_or_benchmark"
            )
            resolved = _filter_self_dependencies(paper, resolved)
            resolved_dataset = _filter_self_dependencies(paper, resolved_dataset)
            resolved = _hydrate_dependency_urls(db, resolved)
            resolved_dataset = _hydrate_dependency_urls(db, resolved_dataset)
            changed = False
            if not resolved and reference_candidates:
                heuristic_raw = _heuristic_key_dependency_records(reference_candidates, max_dependencies=3)
                resolved = _resolve_dependencies(
                    reference_candidates, heuristic_raw, "direct_technical_dependency"
                )
                existing.key_dependencies_json = json.dumps(heuristic_raw)
                changed = True
            if not resolved_dataset and reference_candidates:
                dataset_raw = _heuristic_dataset_dependency_records(reference_candidates, max_dependencies=5)
                resolved_dataset = _resolve_dependencies(
                    reference_candidates, dataset_raw, "dataset_or_benchmark"
                )
                existing.dataset_dependencies_json = json.dumps(dataset_raw)
                changed = True
            needs_method_refresh = _should_refresh_method_dependencies(paper, resolved)
            needs_dataset_refresh = _should_refresh_dataset_dependencies(paper, resolved_dataset)
            if not resolved or not resolved_dataset or needs_method_refresh or needs_dataset_refresh:
                inferred_method, inferred_dataset = _infer_key_dependencies_from_local_db(
                    db, paper, max_method_dependencies=3, max_dataset_dependencies=5
                )
                if (not resolved or needs_method_refresh) and inferred_method:
                    resolved = inferred_method
                    existing.key_dependencies_json = json.dumps(inferred_method)
                    changed = True
                if (not resolved_dataset or needs_dataset_refresh) and inferred_dataset:
                    resolved_dataset = inferred_dataset
                    existing.dataset_dependencies_json = json.dumps(inferred_dataset)
                    changed = True
                resolved = _hydrate_dependency_urls(db, resolved)
                resolved_dataset = _hydrate_dependency_urls(db, resolved_dataset)
            if changed:
                db.commit()
            payload["key_dependencies"] = resolved
            payload["dataset_dependencies"] = resolved_dataset
            return payload

    if cache_only:
        return _empty_analysis()

    generated = generate_paper_analysis(
        {
            "title": paper.title,
            "abstract": paper.abstract,
            "venue": paper.venue,
            "year": paper.year,
            "citation_count": paper.citation_count,
            "url": paper.url,
            "external_id": paper.external_id,
        },
        reference_candidates,
    )
    if not generated:
        return _empty_analysis()

    raw_key_deps = generated.get("key_dependencies", [])
    raw_dataset_deps = generated.get("dataset_dependencies", [])
    resolved_key_deps = _resolve_dependencies(
        reference_candidates, raw_key_deps, "direct_technical_dependency"
    )
    resolved_dataset_deps = _resolve_dependencies(
        reference_candidates, raw_dataset_deps, "dataset_or_benchmark"
    )
    resolved_key_deps = _filter_self_dependencies(paper, resolved_key_deps)
    resolved_dataset_deps = _filter_self_dependencies(paper, resolved_dataset_deps)
    resolved_key_deps = _hydrate_dependency_urls(db, resolved_key_deps)
    resolved_dataset_deps = _hydrate_dependency_urls(db, resolved_dataset_deps)
    if not resolved_key_deps and reference_candidates:
        raw_key_deps = _heuristic_key_dependency_records(reference_candidates, max_dependencies=3)
        resolved_key_deps = _resolve_dependencies(
            reference_candidates, raw_key_deps, "direct_technical_dependency"
        )
        resolved_key_deps = _filter_self_dependencies(paper, resolved_key_deps)
        resolved_key_deps = _hydrate_dependency_urls(db, resolved_key_deps)
    if not resolved_dataset_deps and reference_candidates:
        raw_dataset_deps = _heuristic_dataset_dependency_records(reference_candidates, max_dependencies=5)
        resolved_dataset_deps = _resolve_dependencies(
            reference_candidates, raw_dataset_deps, "dataset_or_benchmark"
        )
        resolved_dataset_deps = _filter_self_dependencies(paper, resolved_dataset_deps)
        resolved_dataset_deps = _hydrate_dependency_urls(db, resolved_dataset_deps)
    if not resolved_key_deps or not resolved_dataset_deps:
        inferred_method, inferred_dataset = _infer_key_dependencies_from_local_db(
            db, paper, max_method_dependencies=3, max_dataset_dependencies=5
        )
        if not resolved_key_deps and inferred_method:
            raw_key_deps = inferred_method
            resolved_key_deps = inferred_method
        if not resolved_dataset_deps and inferred_dataset:
            raw_dataset_deps = inferred_dataset
            resolved_dataset_deps = inferred_dataset
        resolved_key_deps = _hydrate_dependency_urls(db, resolved_key_deps)
        resolved_dataset_deps = _hydrate_dependency_urls(db, resolved_dataset_deps)

    if not existing:
        existing = PaperAnalysis(
            paper_id=paper.id,
            quick_takeaways_json=json.dumps(generated.get("quick_takeaways", [])),
            logic_summary=generated.get("logic_summary", ""),
            evidence_points_json=json.dumps(generated.get("evidence_points", [])),
            limitations_json=json.dumps(generated.get("limitations", [])),
            key_dependencies_json=json.dumps(raw_key_deps),
            dataset_dependencies_json=json.dumps(raw_dataset_deps),
            model_name=generated.get("model_name"),
        )
        db.add(existing)
    else:
        existing.quick_takeaways_json = json.dumps(generated.get("quick_takeaways", []))
        existing.logic_summary = generated.get("logic_summary", "")
        existing.evidence_points_json = json.dumps(generated.get("evidence_points", []))
        existing.limitations_json = json.dumps(generated.get("limitations", []))
        existing.key_dependencies_json = json.dumps(raw_key_deps)
        existing.dataset_dependencies_json = json.dumps(raw_dataset_deps)
        existing.model_name = generated.get("model_name")
    db.commit()

    return {
        "quick_takeaways": generated.get("quick_takeaways", []),
        "logic_summary": generated.get("logic_summary", ""),
        "evidence_points": generated.get("evidence_points", []),
        "limitations": generated.get("limitations", []),
        "key_dependencies": resolved_key_deps,
        "dataset_dependencies": resolved_dataset_deps,
        "model_name": generated.get("model_name"),
    }
