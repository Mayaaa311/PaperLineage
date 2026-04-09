from __future__ import annotations

import json
import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm import generate_paper_analysis
from .models import Paper, PaperAnalysis
from .utils import load_json_list


def _empty_analysis() -> dict:
    return {
        "quick_takeaways": [],
        "logic_summary": "",
        "evidence_points": [],
        "key_dependencies": [],
        "model_name": None,
    }


def _to_payload(entry: PaperAnalysis) -> dict:
    return {
        "quick_takeaways": load_json_list(entry.quick_takeaways_json),
        "logic_summary": entry.logic_summary or "",
        "evidence_points": load_json_list(entry.evidence_points_json),
        "key_dependencies": load_json_list(entry.key_dependencies_json),
        "model_name": entry.model_name,
    }


def _resolve_key_dependencies(ref_candidates: list[dict], picked: list[dict]) -> list[dict]:
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
                    "role": dep.get("role", "direct_technical_dependency"),
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
                "role": dep.get("role", "direct_technical_dependency"),
                "confidence": dep.get("confidence", 0.65),
                "reason": dep.get("reason", ""),
            }
        )
    return out


def _infer_key_dependencies_from_local_db(
    db: Session,
    paper: Paper,
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

    source_tokens = tokenize(paper.title)
    q = select(Paper).where(Paper.id != paper.id)
    if paper.year is not None:
        q = q.where((Paper.year.is_(None)) | (Paper.year <= paper.year))
    rows = list(db.execute(q.order_by(Paper.citation_count.desc()).limit(400)).scalars())
    if not rows:
        return []

    scored: list[tuple[float, Paper]] = []
    for candidate in rows:
        c_tokens = tokenize(candidate.title)
        overlap = len(source_tokens & c_tokens)
        overlap_norm = (overlap / max(1, len(source_tokens))) if source_tokens else 0.0
        citation_component = min(math.log10((candidate.citation_count or 0) + 1) / 3.0, 0.35)
        venue_bonus = 0.08 if (paper.venue and candidate.venue and paper.venue == candidate.venue) else 0.0
        score = 0.15 + overlap_norm * 0.55 + citation_component + venue_bonus
        scored.append((max(0.0, min(1.0, score)), candidate))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x for x in scored[:max_dependencies]]
    if not top:
        top = [(0.58, x) for x in rows[:max_dependencies]]

    out = []
    for score, candidate in top:
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        out.append(
            {
                "id": candidate.id,
                "title": candidate.title,
                "year": candidate.year,
                "venue": candidate.venue,
                "citation_count": candidate.citation_count or 0,
                "role": role,
                "confidence": max(0.55, score),
                "reason": "Inferred from title similarity and citation influence when explicit references were unavailable.",
            }
        )
    return out


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


def get_or_create_paper_analysis(
    db: Session,
    paper: Paper,
    reference_candidates: list[dict],
) -> dict:
    existing = db.execute(select(PaperAnalysis).where(PaperAnalysis.paper_id == paper.id)).scalar_one_or_none()
    if existing:
        payload = _to_payload(existing)
        if payload["quick_takeaways"] and payload["logic_summary"]:
            resolved = _resolve_key_dependencies(
                reference_candidates, payload["key_dependencies"]
            )
            if not resolved and reference_candidates:
                heuristic_raw = _heuristic_key_dependency_records(reference_candidates, max_dependencies=3)
                resolved = _resolve_key_dependencies(reference_candidates, heuristic_raw)
                existing.key_dependencies_json = json.dumps(heuristic_raw)
                db.commit()
            if not resolved:
                inferred = _infer_key_dependencies_from_local_db(db, paper, max_dependencies=3)
                if inferred:
                    resolved = inferred
                    existing.key_dependencies_json = json.dumps(inferred)
                    db.commit()
            payload["key_dependencies"] = resolved
            return payload

    generated = generate_paper_analysis(
        {
            "title": paper.title,
            "abstract": paper.abstract,
            "venue": paper.venue,
            "year": paper.year,
            "citation_count": paper.citation_count,
        },
        reference_candidates,
    )
    if not generated:
        return _empty_analysis()

    raw_key_deps = generated.get("key_dependencies", [])
    resolved_key_deps = _resolve_key_dependencies(reference_candidates, raw_key_deps)
    if not resolved_key_deps and reference_candidates:
        raw_key_deps = _heuristic_key_dependency_records(reference_candidates, max_dependencies=3)
        resolved_key_deps = _resolve_key_dependencies(reference_candidates, raw_key_deps)
    if not resolved_key_deps:
        inferred = _infer_key_dependencies_from_local_db(db, paper, max_dependencies=3)
        if inferred:
            raw_key_deps = inferred
            resolved_key_deps = inferred

    if not existing:
        existing = PaperAnalysis(
            paper_id=paper.id,
            quick_takeaways_json=json.dumps(generated.get("quick_takeaways", [])),
            logic_summary=generated.get("logic_summary", ""),
            evidence_points_json=json.dumps(generated.get("evidence_points", [])),
            key_dependencies_json=json.dumps(raw_key_deps),
            model_name=generated.get("model_name"),
        )
        db.add(existing)
    else:
        existing.quick_takeaways_json = json.dumps(generated.get("quick_takeaways", []))
        existing.logic_summary = generated.get("logic_summary", "")
        existing.evidence_points_json = json.dumps(generated.get("evidence_points", []))
        existing.key_dependencies_json = json.dumps(raw_key_deps)
        existing.model_name = generated.get("model_name")
    db.commit()

    return {
        "quick_takeaways": generated.get("quick_takeaways", []),
        "logic_summary": generated.get("logic_summary", ""),
        "evidence_points": generated.get("evidence_points", []),
        "key_dependencies": resolved_key_deps,
        "model_name": generated.get("model_name"),
    }
