from __future__ import annotations

import json
import math
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal
from .llm import select_key_dependencies
from .models import Paper, PaperAnalysis, TraceGraphEdge, TraceGraphNode, TraceRequest
from .scholar import ScholarClient, normalize_paper
from .utils import upsert_paper

POSITIVE_METHOD_KEYWORDS = {
    "method",
    "model",
    "network",
    "architecture",
    "algorithm",
    "objective",
    "training",
    "attention",
    "transformer",
    "diffusion",
    "backbone",
    "decoder",
    "encoder",
    "optimization",
}

NEGATIVE_KEYWORDS = {
    "dataset",
    "survey",
    "benchmark",
    "tutorial",
    "challenge",
    "workshop",
    "review",
}


def classify_reference(source_paper: Paper, ref: dict) -> tuple[str, float, str, bool]:
    title = (ref.get("title") or "").lower()
    citation_count = ref.get("citation_count", 0) or 0

    score = 0.25
    if citation_count > 0:
        score += min(math.log10(citation_count + 1) / 3.0, 0.25)

    positive_hits = [kw for kw in POSITIVE_METHOD_KEYWORDS if kw in title]
    negative_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in title]
    score += min(0.08 * len(positive_hits), 0.32)
    score -= min(0.12 * len(negative_hits), 0.36)

    source_venue = (source_paper.venue or "").lower()
    ref_venue = (ref.get("venue") or "").lower()
    if source_venue and ref_venue and source_venue == ref_venue:
        score += 0.05

    score = max(0.0, min(1.0, score))

    if negative_hits and "dataset" in negative_hits:
        role = "dataset_or_background"
        reason = "Likely data/benchmark dependency rather than core method."
    elif score >= 0.70:
        role = "foundational_method"
        reason = "Title and influence suggest foundational method dependency."
    elif score >= 0.50:
        role = "direct_technical_dependency"
        reason = "Likely direct technical dependency used in the selected method."
    elif score >= 0.35:
        role = "related_work"
        reason = "Probably relevant related work but weaker direct dependency signal."
    else:
        role = "background"
        reason = "Weak method-dependency signal."

    keep = role in {"foundational_method", "direct_technical_dependency"}
    return role, score, reason, keep


def _upsert_trace_node(db: Session, trace_request_id: int, paper_id: str, level: int) -> None:
    existing = db.execute(
        select(TraceGraphNode).where(
            TraceGraphNode.trace_request_id == trace_request_id,
            TraceGraphNode.paper_id == paper_id,
        )
    ).scalar_one_or_none()
    if not existing:
        db.add(TraceGraphNode(trace_request_id=trace_request_id, paper_id=paper_id, level=level))
        db.flush()
        return
    if level < existing.level:
        existing.level = level
        db.flush()


def _upsert_trace_edge(
    db: Session,
    trace_request_id: int,
    source_paper_id: str,
    target_paper_id: str,
    relation_type: str,
    confidence: float,
    reason: str,
) -> None:
    existing = db.execute(
        select(TraceGraphEdge).where(
            TraceGraphEdge.trace_request_id == trace_request_id,
            TraceGraphEdge.source_paper_id == source_paper_id,
            TraceGraphEdge.target_paper_id == target_paper_id,
        )
    ).scalar_one_or_none()
    if existing:
        return

    db.add(
        TraceGraphEdge(
            trace_request_id=trace_request_id,
            source_paper_id=source_paper_id,
            target_paper_id=target_paper_id,
            relation_type=relation_type,
            confidence=confidence,
            reason=reason,
        )
    )
    db.flush()


def _analysis_dependency_candidates(
    db: Session,
    current: Paper,
    max_dependencies: int = 3,
) -> list[dict]:
    analysis = db.execute(
        select(PaperAnalysis).where(PaperAnalysis.paper_id == current.id)
    ).scalar_one_or_none()
    if not analysis or not analysis.key_dependencies_json:
        return []
    try:
        deps = json.loads(analysis.key_dependencies_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(deps, list):
        return []

    out: list[dict] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        paper_id = str(dep.get("id") or "").strip()
        if not paper_id:
            continue
        ref_paper = db.execute(select(Paper).where(Paper.id == paper_id)).scalar_one_or_none()
        if not ref_paper:
            continue
        out.append(
            {
                "paper_id": ref_paper.id,
                "role": str(dep.get("role") or "direct_technical_dependency"),
                "score": float(dep.get("confidence") or 0.6),
                "reason": str(
                    dep.get("reason")
                    or "Recovered from cached detail dependency inference."
                ),
            }
        )
        if len(out) >= max_dependencies:
            break
    return out


def _local_similarity_candidates(
    db: Session,
    current: Paper,
    max_dependencies: int = 3,
) -> list[dict]:
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
    }

    def tokenize(text: str | None) -> set[str]:
        tokens = [x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(x) >= 4]
        return {x for x in tokens if x not in stopwords}

    source_tokens = tokenize(current.title)
    q = select(Paper).where(Paper.id != current.id)
    if current.year is not None:
        q = q.where((Paper.year.is_(None)) | (Paper.year <= current.year))
    rows = list(db.execute(q.order_by(Paper.citation_count.desc()).limit(400)).scalars())
    if not rows:
        return []

    scored: list[tuple[float, Paper]] = []
    for candidate in rows:
        c_tokens = tokenize(candidate.title)
        overlap = len(source_tokens & c_tokens)
        overlap_norm = (overlap / max(1, len(source_tokens))) if source_tokens else 0.0
        citation_component = min(math.log10((candidate.citation_count or 0) + 1) / 3.0, 0.35)
        venue_bonus = 0.08 if (current.venue and candidate.venue and current.venue == candidate.venue) else 0.0
        score = 0.15 + overlap_norm * 0.55 + citation_component + venue_bonus
        scored.append((max(0.0, min(1.0, score)), candidate))

    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[dict] = []
    for score, candidate in scored[:max_dependencies]:
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        out.append(
            {
                "paper_id": candidate.id,
                "role": role,
                "score": max(0.55, score),
                "reason": "Inferred from title similarity and citation influence when explicit references were unavailable.",
            }
        )
    return out


def run_trace_job(trace_request_id: int) -> None:
    db = SessionLocal()
    scholar = ScholarClient()
    try:
        trace_req = db.execute(
            select(TraceRequest).where(TraceRequest.id == trace_request_id)
        ).scalar_one_or_none()
        if not trace_req:
            return

        root_paper = db.execute(
            select(Paper).where(Paper.id == trace_req.root_paper_id)
        ).scalar_one_or_none()
        if not root_paper:
            trace_req.status = "failed"
            trace_req.error_message = "Root paper not found."
            db.commit()
            return

        trace_req.status = "running"
        db.commit()

        _upsert_trace_node(db, trace_req.id, root_paper.id, 0)
        db.commit()

        visited: set[str] = {root_paper.id}

        def walk(current: Paper, level: int) -> None:
            if level >= trace_req.trace_depth:
                return

            candidates: list[dict] = []
            if current.external_id:
                try:
                    payload = scholar.get_paper(current.external_id)
                except Exception:
                    payload = None
                if payload:
                    normalized_current = normalize_paper(payload)
                    references = normalized_current.get("references", [])
                    for raw_ref in references:
                        normalized_ref = normalize_paper(raw_ref)
                        if not normalized_ref.get("external_id") or not normalized_ref.get("title"):
                            continue
                        candidates.append(normalized_ref)

            selected = select_key_dependencies(
                source_paper={
                    "title": current.title,
                    "abstract": current.abstract,
                    "venue": current.venue,
                    "year": current.year,
                },
                references=candidates,
                max_dependencies=3,
            )

            if not selected:
                scored_candidates = []
                for normalized_ref in candidates:
                    role, score, reason, keep = classify_reference(current, normalized_ref)
                    if not keep:
                        continue
                    scored_candidates.append(
                        {
                            "paper": normalized_ref,
                            "role": role,
                            "score": score,
                            "reason": reason,
                        }
                    )
                scored_candidates.sort(key=lambda x: x["score"], reverse=True)
                selected = scored_candidates[:3]

            if not selected:
                selected = _analysis_dependency_candidates(db, current, max_dependencies=3)

            if not selected:
                selected = _local_similarity_candidates(db, current, max_dependencies=3)

            selected = selected[:3]

            for candidate in selected:
                ref_paper = None
                if candidate.get("paper_id"):
                    ref_paper = db.execute(
                        select(Paper).where(Paper.id == candidate["paper_id"])
                    ).scalar_one_or_none()
                if not ref_paper:
                    paper_payload = candidate.get("paper")
                    if not isinstance(paper_payload, dict):
                        continue
                    ref_paper = upsert_paper(db, paper_payload)
                _upsert_trace_node(db, trace_req.id, ref_paper.id, level + 1)
                _upsert_trace_edge(
                    db,
                    trace_req.id,
                    current.id,
                    ref_paper.id,
                    candidate["role"],
                    candidate["score"],
                    candidate["reason"],
                )
                db.commit()

                if ref_paper.id in visited:
                    continue
                visited.add(ref_paper.id)
                walk(ref_paper, level + 1)

        walk(root_paper, 0)
        trace_req.status = "completed"
        trace_req.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        trace_req = db.execute(
            select(TraceRequest).where(TraceRequest.id == trace_request_id)
        ).scalar_one_or_none()
        if trace_req:
            trace_req.status = "failed"
            trace_req.error_message = str(exc)
            db.commit()
    finally:
        db.close()
