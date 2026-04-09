from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

import httpx

from .llm_cache import get_cached_json, set_cached_json

OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/chat/completions")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

METHOD_KEYWORDS = {
    "method",
    "model",
    "architecture",
    "algorithm",
    "training",
    "objective",
    "transformer",
    "diffusion",
    "backbone",
    "encoder",
    "decoder",
}

NON_METHOD_KEYWORDS = {
    "dataset",
    "benchmark",
    "survey",
    "review",
    "challenge",
}


def _analysis_ref_score(item: dict) -> float:
    title = str(item.get("title") or "").lower()
    abstract = str(item.get("abstract") or "").lower()
    text = f"{title} {abstract}"
    citation_count = item.get("citation_count", 0) or 0

    score = 0.2 + min(math.log10(citation_count + 1) / 3.0, 0.3)
    score += min(sum(1 for x in METHOD_KEYWORDS if x in text) * 0.07, 0.35)
    score -= min(sum(1 for x in NON_METHOD_KEYWORDS if x in text) * 0.1, 0.25)
    return max(0.0, min(1.0, score))


def _heuristic_analysis_dependencies(ref_items: list[dict], max_dependencies: int = 3) -> list[dict]:
    if not ref_items:
        return []
    scored: list[tuple[float, dict]] = []
    for item in ref_items:
        if not item.get("ref_id") or not item.get("title"):
            continue
        scored.append((_analysis_ref_score(item), item))
    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = scored[:max_dependencies]
    out = []
    for score, item in selected:
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        reason = (
            "Likely foundational method paper based on technical relevance and citation impact."
            if role == "foundational_method"
            else "Likely direct technical dependency based on method relevance."
        )
        out.append(
            {
                "ref_id": item["ref_id"],
                "role": role,
                "confidence": max(0.55, score),
                "reason": reason,
            }
        )
    return out


def has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def _extract_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def _chat_json(system_prompt: str, user_prompt: str, max_tokens: int = 1100) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {}

    cache_payload = json.dumps(
        {
            "model": OPENAI_MODEL,
            "temperature": 0.2,
            "response_format": "json_object",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
        },
        sort_keys=True,
    )
    cache_key = hashlib.sha256(cache_payload.encode("utf-8")).hexdigest()
    cached = get_cached_json(cache_key)
    if cached:
        return cached

    payload = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=httpx.Timeout(45.0)) as client:
        response = client.post(OPENAI_API_URL, headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        parsed = _extract_json_object(content)
        if parsed:
            set_cached_json(cache_key, parsed)
        return parsed


def _heuristic_dependency_score(reference: dict) -> float:
    title = (reference.get("title") or "").lower()
    citation_count = reference.get("citation_count", 0) or 0
    score = 0.2 + min(math.log10(citation_count + 1) / 3.0, 0.25)
    score += min(sum(1 for x in METHOD_KEYWORDS if x in title) * 0.08, 0.32)
    score -= min(sum(1 for x in NON_METHOD_KEYWORDS if x in title) * 0.1, 0.3)
    return max(0.0, min(1.0, score))


def _heuristic_select_dependencies(references: list[dict], max_dependencies: int = 3) -> list[dict]:
    candidates = []
    for ref in references:
        if not ref.get("external_id") or not ref.get("title"):
            continue
        score = _heuristic_dependency_score(ref)
        role = "foundational_method" if score >= 0.68 else "direct_technical_dependency"
        if score < 0.45:
            continue
        candidates.append(
            {
                "paper": ref,
                "role": role,
                "score": score,
                "reason": "High-citation method-related reference based on title signals.",
            }
        )
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:max_dependencies]


def select_key_dependencies(
    source_paper: dict,
    references: list[dict],
    max_dependencies: int = 3,
) -> list[dict]:
    valid_refs = [x for x in references if x.get("external_id") and x.get("title")]
    if not valid_refs:
        return []

    ranked_refs = sorted(valid_refs, key=lambda x: x.get("citation_count", 0) or 0, reverse=True)[:35]
    ref_items = []
    ref_map: dict[str, dict] = {}
    for i, ref in enumerate(ranked_refs, start=1):
        rid = f"r{i}"
        ref_map[rid] = ref
        ref_items.append(
            {
                "ref_id": rid,
                "title": ref.get("title"),
                "year": ref.get("year"),
                "venue": ref.get("venue"),
                "citation_count": ref.get("citation_count", 0),
                "abstract": (ref.get("abstract") or "")[:280],
            }
        )

    if not has_openai_key():
        return _heuristic_select_dependencies(ranked_refs, max_dependencies=max_dependencies)

    system_prompt = (
        "You identify direct technical dependencies in citations. "
        "Select only references that the source paper likely relies on methodologically. "
        "Avoid generic surveys, benchmark-only, and peripheral related work. "
        "Return strict JSON."
    )
    user_prompt = json.dumps(
        {
            "task": "Pick the most relied-on prior papers for this source paper.",
            "max_dependencies": max_dependencies,
            "source_paper": {
                "title": source_paper.get("title"),
                "abstract": (source_paper.get("abstract") or "")[:1200],
                "venue": source_paper.get("venue"),
                "year": source_paper.get("year"),
            },
            "candidate_references": ref_items,
            "output_schema": {
                "selected": [
                    {
                        "ref_id": "r1",
                        "role": "foundational_method | direct_technical_dependency",
                        "confidence": 0.0,
                        "reason": "short reason",
                    }
                ]
            },
        }
    )

    try:
        parsed = _chat_json(system_prompt, user_prompt, max_tokens=900)
    except Exception:
        return _heuristic_select_dependencies(ranked_refs, max_dependencies=max_dependencies)

    selected = parsed.get("selected")
    if not isinstance(selected, list):
        return _heuristic_select_dependencies(ranked_refs, max_dependencies=max_dependencies)

    out: list[dict] = []
    seen_ids: set[str] = set()
    for item in selected:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("ref_id") or "")
        ref = ref_map.get(rid)
        if not ref:
            continue
        ext_id = ref.get("external_id")
        if not ext_id or ext_id in seen_ids:
            continue
        seen_ids.add(ext_id)
        role_raw = str(item.get("role") or "direct_technical_dependency").lower()
        role = "foundational_method" if "foundational" in role_raw else "direct_technical_dependency"
        confidence = item.get("confidence", 0.65)
        try:
            score = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            score = 0.65
        reason = str(item.get("reason") or "Core method dependency.")
        out.append({"paper": ref, "role": role, "score": score, "reason": reason})
        if len(out) >= max_dependencies:
            break

    if not out:
        return _heuristic_select_dependencies(ranked_refs, max_dependencies=max_dependencies)
    return out


def generate_paper_analysis(paper: dict, references: list[dict]) -> dict[str, Any]:
    refs_ranked = sorted(references, key=lambda x: x.get("citation_count", 0) or 0, reverse=True)[:20]
    ref_items = []
    ref_ids = set()
    for i, ref in enumerate(refs_ranked, start=1):
        rid = str(ref.get("id") or f"p{i}")
        ref_ids.add(rid)
        ref_items.append(
            {
                "ref_id": rid,
                "title": ref.get("title"),
                "year": ref.get("year"),
                "venue": ref.get("venue"),
                "citation_count": ref.get("citation_count", 0),
                "abstract": (ref.get("abstract") or "")[:260],
            }
        )

    heuristic_deps = _heuristic_analysis_dependencies(ref_items, max_dependencies=3)
    fallback = {
        "quick_takeaways": [
            "Problem: The paper addresses a specific task bottleneck described in the abstract.",
            "Gap: Prior methods have performance, scalability, or generalization limitations.",
            "Method: The paper proposes a new modeling/training strategy to close that gap.",
        ],
        "logic_summary": "The paper motivates a method change, validates it against baselines, then supports claims with ablations and robustness checks.",
        "evidence_points": [
            "Main experiments compare against prior baselines on standard benchmarks.",
            "Metrics are task-specific and chosen to measure quality/performance tradeoffs.",
            "Ablation studies isolate important components and show contribution of each part.",
        ],
        "key_dependencies": heuristic_deps,
        "model_name": "heuristic-fallback",
    }

    if not has_openai_key():
        return fallback

    system_prompt = (
        "You are a concise ML paper analyst. "
        "Extract problem, gap, and method in three bullets; then summarize argumentative logic and evidence. "
        "Return strict JSON only."
    )
    user_prompt = json.dumps(
        {
            "task": "Summarize this paper for a reader and identify 1-3 key relied-on references.",
            "paper": {
                "title": paper.get("title"),
                "abstract": (paper.get("abstract") or "")[:1600],
                "venue": paper.get("venue"),
                "year": paper.get("year"),
                "citation_count": paper.get("citation_count", 0),
            },
            "candidate_references": ref_items,
            "requirements": {
                "quick_takeaways": [
                    "Problem: ...",
                    "Gap: ...",
                    "Method: ...",
                ],
                "logic_summary": "3-5 concise sentences.",
                "evidence_points": [
                    "Mention tasks/datasets/metrics if available.",
                    "Mention ablations or stress tests if available.",
                ],
                "key_dependencies": [
                    {
                        "ref_id": "paper local id",
                        "role": "foundational_method | direct_technical_dependency",
                        "confidence": 0.0,
                        "reason": "one short sentence",
                    }
                ],
            },
            "constraints": {
                "max_key_dependencies": 3,
                "evidence_points_count": 4,
                "concise": True,
            },
        }
    )

    try:
        parsed = _chat_json(system_prompt, user_prompt, max_tokens=1200)
    except Exception:
        return fallback

    quick = parsed.get("quick_takeaways")
    logic_summary = parsed.get("logic_summary")
    evidence = parsed.get("evidence_points")
    dependencies = parsed.get("key_dependencies")

    if not isinstance(quick, list):
        quick = fallback["quick_takeaways"]
    quick = [str(x).strip() for x in quick if str(x).strip()][:3]
    if len(quick) < 3:
        quick = fallback["quick_takeaways"]

    if not isinstance(logic_summary, str) or not logic_summary.strip():
        logic_summary = fallback["logic_summary"]

    if not isinstance(evidence, list):
        evidence = fallback["evidence_points"]
    evidence = [str(x).strip() for x in evidence if str(x).strip()][:6]
    if not evidence:
        evidence = fallback["evidence_points"]

    normalized_deps = []
    if isinstance(dependencies, list):
        for dep in dependencies:
            if not isinstance(dep, dict):
                continue
            ref_id = str(dep.get("ref_id") or "")
            if ref_id not in ref_ids:
                continue
            role_raw = str(dep.get("role") or "direct_technical_dependency").lower()
            role = "foundational_method" if "foundational" in role_raw else "direct_technical_dependency"
            try:
                confidence = max(0.0, min(1.0, float(dep.get("confidence", 0.65))))
            except (TypeError, ValueError):
                confidence = 0.65
            normalized_deps.append(
                {
                    "ref_id": ref_id,
                    "role": role,
                    "confidence": confidence,
                    "reason": str(dep.get("reason") or "Core method dependency."),
                }
            )
            if len(normalized_deps) >= 3:
                break
    if not normalized_deps:
        normalized_deps = heuristic_deps

    return {
        "quick_takeaways": quick,
        "logic_summary": logic_summary.strip(),
        "evidence_points": evidence,
        "key_dependencies": normalized_deps,
        "model_name": OPENAI_MODEL,
    }
