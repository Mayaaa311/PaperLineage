from __future__ import annotations

import hashlib
import json
import math
import os
import re
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

DATASET_KEYWORDS = {
    "dataset",
    "benchmark",
    "corpus",
    "challenge",
    "evaluation",
    "test set",
}

DEPENDENCY_STOPWORDS = {
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


def _tokenize_terms(text: str | None) -> set[str]:
    tokens = [x for x in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(x) >= 3]
    return {x for x in tokens if x not in DEPENDENCY_STOPWORDS}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _local_method_score(source_paper: dict, candidate: dict) -> tuple[float, list[str]]:
    source_title_tokens = _tokenize_terms(source_paper.get("title"))
    source_context_tokens = _tokenize_terms(
        f"{source_paper.get('title') or ''} {source_paper.get('abstract') or ''}"
    )
    cand_title_tokens = _tokenize_terms(candidate.get("title"))
    cand_context_tokens = _tokenize_terms(
        f"{candidate.get('title') or ''} {candidate.get('abstract') or ''}"
    )
    cand_text = f"{candidate.get('title') or ''} {candidate.get('abstract') or ''}".lower()
    citation_count = candidate.get("citation_count", 0) or 0

    title_overlap = (
        len(source_title_tokens & cand_title_tokens) / max(1, len(source_title_tokens))
        if source_title_tokens
        else 0.0
    )
    context_overlap = (
        len(source_context_tokens & cand_context_tokens) / max(1, len(source_context_tokens))
        if source_context_tokens
        else 0.0
    )
    method_kw_hits = sum(1 for kw in METHOD_KEYWORDS if kw in cand_text)
    non_method_hits = sum(1 for kw in NON_METHOD_KEYWORDS if kw in cand_text)
    citation_component = min(math.log10(citation_count + 1) / 3.0, 0.28)

    score = (
        0.14
        + title_overlap * 0.36
        + context_overlap * 0.24
        + min(method_kw_hits * 0.06, 0.2)
        + citation_component
        - min(non_method_hits * 0.08, 0.22)
    )
    overlap_terms = sorted(source_context_tokens & cand_context_tokens)
    return _clamp(score), overlap_terms[:3]


def _local_dataset_score(source_paper: dict, candidate: dict) -> tuple[float, list[str]]:
    source_text = f"{source_paper.get('title') or ''} {source_paper.get('abstract') or ''}".lower()
    cand_text = f"{candidate.get('title') or ''} {candidate.get('abstract') or ''}".lower()
    source_tokens = _tokenize_terms(source_text)
    cand_tokens = _tokenize_terms(cand_text)
    citation_count = candidate.get("citation_count", 0) or 0

    dataset_hits = sum(1 for kw in DATASET_KEYWORDS if kw in cand_text)
    source_dataset_hits = sum(1 for kw in DATASET_KEYWORDS if kw in source_text and kw in cand_text)
    context_overlap = len(source_tokens & cand_tokens) / max(1, len(source_tokens)) if source_tokens else 0.0
    citation_component = min(math.log10(citation_count + 1) / 3.0, 0.22)
    method_penalty = min(sum(1 for kw in METHOD_KEYWORDS if kw in cand_text) * 0.04, 0.16)

    score = (
        0.12
        + min(dataset_hits * 0.12, 0.42)
        + min(source_dataset_hits * 0.1, 0.2)
        + context_overlap * 0.12
        + citation_component
        - method_penalty
    )
    overlap_terms = sorted(source_tokens & cand_tokens)
    return _clamp(score), overlap_terms[:3]


def _analysis_ref_score(item: dict) -> float:
    title = str(item.get("title") or "").lower()
    abstract = str(item.get("abstract") or "").lower()
    text = f"{title} {abstract}"
    citation_count = item.get("citation_count", 0) or 0

    score = 0.2 + min(math.log10(citation_count + 1) / 3.0, 0.3)
    score += min(sum(1 for x in METHOD_KEYWORDS if x in text) * 0.07, 0.35)
    score -= min(sum(1 for x in NON_METHOD_KEYWORDS if x in text) * 0.1, 0.25)
    return max(0.0, min(1.0, score))


def _analysis_dataset_score(item: dict) -> float:
    title = str(item.get("title") or "").lower()
    abstract = str(item.get("abstract") or "").lower()
    text = f"{title} {abstract}"
    citation_count = item.get("citation_count", 0) or 0

    score = 0.18 + min(math.log10(citation_count + 1) / 3.0, 0.24)
    score += min(sum(1 for x in DATASET_KEYWORDS if x in text) * 0.12, 0.52)
    score -= min(sum(1 for x in METHOD_KEYWORDS if x in text) * 0.05, 0.2)
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


def _heuristic_analysis_dataset_dependencies(ref_items: list[dict], max_dependencies: int = 5) -> list[dict]:
    if not ref_items:
        return []
    scored: list[tuple[float, dict]] = []
    for item in ref_items:
        if not item.get("ref_id") or not item.get("title"):
            continue
        scored.append((_analysis_dataset_score(item), item))
    if not scored:
        return []

    scored.sort(key=lambda x: x[0], reverse=True)
    selected = [x for x in scored if x[0] >= 0.48][:max_dependencies]
    out = []
    for score, item in selected:
        out.append(
            {
                "ref_id": item["ref_id"],
                "role": "dataset_or_benchmark",
                "confidence": max(0.5, score),
                "reason": "Likely evaluation dataset/benchmark dependency based on title/abstract cues.",
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


def _split_sentences(text: str | None) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = re.split(r"(?<=[.!?])\s+", raw)
    return [x.strip() for x in parts if len(x.strip()) >= 20][:14]


def _pick_sentence(sentences: list[str], keywords: list[str], used: set[int]) -> tuple[int | None, str]:
    for i, sent in enumerate(sentences):
        if i in used:
            continue
        lower = sent.lower()
        if any(k in lower for k in keywords):
            used.add(i)
            return i, sent
    for i, sent in enumerate(sentences):
        if i in used:
            continue
        used.add(i)
        return i, sent
    return None, ""


def _build_sectioned_fallback_analysis(paper: dict) -> dict[str, Any]:
    abstract = (paper.get("abstract") or "").strip()
    sentences = _split_sentences(abstract)
    used: set[int] = set()

    _, problem_sent = _pick_sentence(
        sentences,
        ["problem", "challenge", "limitation", "bottleneck", "gap", "however", "remain", "task"],
        used,
    )
    _, method_sent = _pick_sentence(
        sentences,
        ["we propose", "we introduce", "we design", "method", "architecture", "framework", "model"],
        used,
    )
    _, eval_sent = _pick_sentence(
        sentences,
        ["experiment", "evaluate", "benchmark", "dataset", "results", "outperform", "compare"],
        used,
    )
    _, ablation_sent = _pick_sentence(
        sentences,
        ["ablation", "analysis", "robust", "sensitivity", "component", "contribution", "effect"],
        used,
    )

    if not problem_sent and sentences:
        problem_sent = sentences[0]
    if not method_sent and len(sentences) > 1:
        method_sent = sentences[1]
    if not eval_sent and len(sentences) > 2:
        eval_sent = sentences[2]

    quick_takeaways = [
        f"Problem: {problem_sent or 'The paper targets a core bottleneck in the stated task.'}",
        "Gap: Prior approaches are described as insufficient for robustness, controllability, or generalization.",
        f"Method: {method_sent or 'The paper proposes a new modeling/training design to close the gap.'}",
    ]

    logic_summary = (
        f"Section 1 (Introduction/Motivation) argues the central problem and research gap: {problem_sent or 'the task remains under-served by existing methods'}. "
        f"Section 2 (Method) argues the proposed mechanism addresses that gap: {method_sent or 'a new architecture/objective is introduced'}. "
        f"Section 3 (Main Evaluation) argues empirical validity through benchmark comparisons: {eval_sent or 'the method is evaluated against strong baselines'}. "
        f"Section 4 (Analysis/Ablation) argues which components are necessary and whether gains are stable: {ablation_sent or 'component-level contribution and robustness should be verified in ablation/analysis sections'}."
    )

    evidence_points = [
        f"[Section: Introduction/Motivation] Argues why the problem matters and what prior methods fail to resolve: {problem_sent or 'motivation and gap framing.'}",
        f"[Section: Method] Argues the technical mechanism and why it should work: {method_sent or 'method design and expected advantage.'}",
        f"[Section: Main Evaluation] Argues effectiveness via benchmark/baseline comparisons: {eval_sent or 'main quantitative results and comparisons.'}",
        f"[Section: Ablation/Analysis] Argues contribution of each module and robustness of claims: {ablation_sent or 'ablation and stress-test evidence.'}",
    ]
    return {
        "quick_takeaways": quick_takeaways,
        "logic_summary": logic_summary,
        "evidence_points": evidence_points,
    }


def _build_limitations_fallback(paper: dict) -> list[str]:
    sentences = _split_sentences(paper.get("abstract"))
    if not sentences:
        return [
            "The abstract does not report clear failure modes; check discussion/appendix for explicit limitations.",
        ]
    limitation_keywords = [
        "however",
        "limitation",
        "challenge",
        "restrict",
        "cost",
        "compute",
        "data",
        "unseen",
        "generalization",
        "future work",
        "remain",
    ]
    hits = []
    for sent in sentences:
        lower = sent.lower()
        if any(k in lower for k in limitation_keywords):
            hits.append(sent)
        if len(hits) >= 2:
            break
    if hits:
        return [f"Potential limitation inferred from abstract: {x}" for x in hits[:2]]
    return [
        "Potential limitation: evidence in the abstract may not cover all domains/settings; verify robustness sections for boundary conditions.",
    ]


def _ensure_sectioned_evidence(evidence: list[str]) -> list[str]:
    section_labels = [
        "[Section: Introduction/Motivation]",
        "[Section: Method]",
        "[Section: Main Evaluation]",
        "[Section: Ablation/Analysis]",
    ]
    out: list[str] = []
    for idx, item in enumerate(evidence):
        text = str(item or "").strip()
        if not text:
            continue
        if text.lower().startswith("[section:"):
            out.append(text)
            continue
        label = section_labels[min(idx, len(section_labels) - 1)]
        out.append(f"{label} {text}")
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
    heuristic_dataset_deps = _heuristic_analysis_dataset_dependencies(ref_items, max_dependencies=5)
    sectioned_fallback = _build_sectioned_fallback_analysis(paper)
    limitations_fallback = _build_limitations_fallback(paper)
    fallback = {
        "quick_takeaways": sectioned_fallback["quick_takeaways"],
        "logic_summary": sectioned_fallback["logic_summary"],
        "evidence_points": sectioned_fallback["evidence_points"],
        "limitations": limitations_fallback,
        "key_dependencies": heuristic_deps,
        "dataset_dependencies": heuristic_dataset_deps,
        "model_name": "heuristic-fallback",
    }

    if not has_openai_key():
        return fallback

    system_prompt = (
        "You are a concise ML paper analyst. "
        "Extract problem, gap, and method in three bullets; then summarize argumentative logic and evidence. "
        "When identifying prior work, distinguish methodological dependencies vs dataset/benchmark papers. "
        "Prioritize cues from introduction/problem framing and method description. "
        "For evidence points, explicitly name section roles (Introduction, Method, Main Evaluation, Ablation/Analysis) "
        "and state what each section argues for. "
        "Also output 1-3 concrete limitations/risks grounded in paper scope, assumptions, or evaluation coverage. "
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
                "logic_summary": "4-6 sentences and mention section-level logic (Introduction, Method, Evaluation, Analysis/Ablation).",
                "evidence_points": [
                    "[Section: Introduction/Motivation] what this section argues for.",
                    "[Section: Method] what this section argues for.",
                    "[Section: Main Evaluation] datasets/tasks/metrics and what they prove.",
                    "[Section: Ablation/Analysis] what components/claims are validated.",
                ],
                "limitations": [
                    "1-3 concise limits of scope/assumptions/data coverage or robustness.",
                ],
                "key_dependencies": [
                    {
                        "ref_id": "paper local id",
                        "role": "foundational_method | direct_technical_dependency",
                        "confidence": 0.0,
                        "reason": "one short sentence",
                    }
                ],
                "dataset_dependencies": [
                    {
                        "ref_id": "paper local id",
                        "role": "dataset_or_benchmark",
                        "confidence": 0.0,
                        "reason": "one short sentence about evaluation data/benchmark usage",
                    }
                ],
            },
            "constraints": {
                "max_key_dependencies": 3,
                "max_dataset_dependencies": 6,
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
    limitations = parsed.get("limitations")
    dependencies = parsed.get("key_dependencies")
    dataset_dependencies = parsed.get("dataset_dependencies")

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
    evidence = _ensure_sectioned_evidence(evidence)
    if not evidence:
        evidence = fallback["evidence_points"]

    normalized_limitations = []
    if isinstance(limitations, list):
        normalized_limitations = [str(x).strip() for x in limitations if str(x).strip()][:3]
    if not normalized_limitations:
        normalized_limitations = limitations_fallback

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

    normalized_dataset_deps = []
    if isinstance(dataset_dependencies, list):
        for dep in dataset_dependencies:
            if not isinstance(dep, dict):
                continue
            ref_id = str(dep.get("ref_id") or "")
            if ref_id not in ref_ids:
                continue
            try:
                confidence = max(0.0, min(1.0, float(dep.get("confidence", 0.62))))
            except (TypeError, ValueError):
                confidence = 0.62
            normalized_dataset_deps.append(
                {
                    "ref_id": ref_id,
                    "role": "dataset_or_benchmark",
                    "confidence": confidence,
                    "reason": str(
                        dep.get("reason")
                        or "Likely dataset/benchmark dependency used in evaluation."
                    ),
                }
            )
            if len(normalized_dataset_deps) >= 6:
                break
    if not normalized_dataset_deps:
        normalized_dataset_deps = heuristic_dataset_deps

    return {
        "quick_takeaways": quick,
        "logic_summary": logic_summary.strip(),
        "evidence_points": evidence,
        "limitations": normalized_limitations,
        "key_dependencies": normalized_deps,
        "dataset_dependencies": normalized_dataset_deps,
        "model_name": OPENAI_MODEL,
    }


def infer_local_dependencies(
    source_paper: dict,
    candidate_papers: list[dict],
    max_method: int = 3,
    max_dataset: int = 5,
) -> dict[str, list[dict]]:
    if not candidate_papers:
        return {"method_dependencies": [], "dataset_dependencies": []}

    cleaned: list[dict] = []
    for item in candidate_papers:
        pid = str(item.get("id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not pid or not title:
            continue
        cleaned.append(
            {
                "id": pid,
                "title": title,
                "abstract": (item.get("abstract") or "")[:320],
                "venue": item.get("venue"),
                "year": item.get("year"),
                "citation_count": item.get("citation_count", 0) or 0,
            }
        )
    if not cleaned:
        return {"method_dependencies": [], "dataset_dependencies": []}

    # Heuristic fallback candidates with source-aware scoring.
    method_scores: dict[str, tuple[float, list[str]]] = {}
    dataset_scores: dict[str, tuple[float, list[str]]] = {}
    for item in cleaned:
        method_scores[item["id"]] = _local_method_score(source_paper, item)
        dataset_scores[item["id"]] = _local_dataset_score(source_paper, item)
    method_scored = sorted(cleaned, key=lambda x: method_scores[x["id"]][0], reverse=True)
    dataset_scored = sorted(cleaned, key=lambda x: dataset_scores[x["id"]][0], reverse=True)
    heuristic_method = []
    for x in method_scored[:max_method]:
        score, terms = method_scores[x["id"]]
        role = "foundational_method" if score >= 0.7 else "direct_technical_dependency"
        if terms:
            reason = (
                f"Matched introduction/method cues ({', '.join(terms)}) and citation influence."
            )
        else:
            reason = "Inferred from source-title/method overlap and citation influence."
        heuristic_method.append(
            {
                "id": x["id"],
                "role": role,
                "confidence": max(0.55, score),
                "reason": reason,
            }
        )
    heuristic_dataset = []
    for x in dataset_scored:
        score, terms = dataset_scores[x["id"]]
        if score < 0.48:
            continue
        reason = (
            f"Matched evaluation/data cues ({', '.join(terms)})." if terms else "Inferred from evaluation/data cues in title and abstract."
        )
        heuristic_dataset.append(
            {
                "id": x["id"],
                "role": "dataset_or_benchmark",
                "confidence": max(0.5, score),
                "reason": reason,
            }
        )
        if len(heuristic_dataset) >= max_dataset:
            break

    if not has_openai_key():
        return {
            "method_dependencies": heuristic_method,
            "dataset_dependencies": heuristic_dataset,
        }

    compact = []
    for x in cleaned[:80]:
        compact.append(
            {
                "id": x["id"],
                "title": x["title"],
                "abstract": x["abstract"],
                "venue": x["venue"],
                "year": x["year"],
                "citation_count": x["citation_count"],
            }
        )

    system_prompt = (
        "You identify prior-paper roles for a source paper. "
        "Pick method dependencies and dataset/benchmark papers separately. "
        "Use introduction/problem framing and method/evaluation cues from provided text. "
        "Return strict JSON only."
    )
    user_prompt = json.dumps(
        {
            "task": "Select method and dataset dependencies from local candidate papers.",
            "source_paper": {
                "title": source_paper.get("title"),
                "abstract": (source_paper.get("abstract") or "")[:1400],
                "venue": source_paper.get("venue"),
                "year": source_paper.get("year"),
            },
            "candidate_papers": compact,
            "output_schema": {
                "method_dependencies": [
                    {
                        "id": "paper id",
                        "role": "foundational_method | direct_technical_dependency",
                        "confidence": 0.0,
                        "reason": "short reason",
                    }
                ],
                "dataset_dependencies": [
                    {
                        "id": "paper id",
                        "role": "dataset_or_benchmark",
                        "confidence": 0.0,
                        "reason": "short reason",
                    }
                ],
            },
            "constraints": {
                "max_method": max_method,
                "max_dataset": max_dataset,
            },
        }
    )

    try:
        parsed = _chat_json(system_prompt, user_prompt, max_tokens=900)
    except Exception:
        return {
            "method_dependencies": heuristic_method,
            "dataset_dependencies": heuristic_dataset,
        }

    candidates_by_id = {x["id"]: x for x in compact}
    out_method: list[dict] = []
    out_dataset: list[dict] = []

    raw_method = parsed.get("method_dependencies")
    if isinstance(raw_method, list):
        for dep in raw_method:
            if not isinstance(dep, dict):
                continue
            did = str(dep.get("id") or "").strip()
            if did not in candidates_by_id:
                continue
            role_raw = str(dep.get("role") or "direct_technical_dependency").lower()
            role = "foundational_method" if "foundational" in role_raw else "direct_technical_dependency"
            raw_conf = _safe_float(dep.get("confidence", 0.65), 0.65)
            lexical_score, overlap_terms = method_scores.get(did) or _local_method_score(
                source_paper,
                candidates_by_id[did],
            )
            confidence = _clamp(raw_conf * 0.55 + lexical_score * 0.45)
            reason = str(dep.get("reason") or "").strip()
            if not reason:
                if overlap_terms:
                    reason = f"Matched introduction/method cues ({', '.join(overlap_terms)})."
                else:
                    reason = "Method dependency inferred from introduction and technical overlap."
            out_method.append(
                {
                    "id": did,
                    "role": role,
                    "confidence": confidence,
                    "reason": reason,
                }
            )
            if len(out_method) >= max_method:
                break

    raw_dataset = parsed.get("dataset_dependencies")
    if isinstance(raw_dataset, list):
        for dep in raw_dataset:
            if not isinstance(dep, dict):
                continue
            did = str(dep.get("id") or "").strip()
            if did not in candidates_by_id:
                continue
            raw_conf = _safe_float(dep.get("confidence", 0.6), 0.6)
            lexical_score, overlap_terms = dataset_scores.get(did) or _local_dataset_score(
                source_paper,
                candidates_by_id[did],
            )
            confidence = _clamp(raw_conf * 0.6 + lexical_score * 0.4)
            reason = str(dep.get("reason") or "").strip()
            if not reason:
                if overlap_terms:
                    reason = f"Matched evaluation/data cues ({', '.join(overlap_terms)})."
                else:
                    reason = "Dataset/benchmark dependency inferred from evaluation cues."
            out_dataset.append(
                {
                    "id": did,
                    "role": "dataset_or_benchmark",
                    "confidence": confidence,
                    "reason": reason,
                }
            )
            if len(out_dataset) >= max_dataset:
                break

    if not out_method:
        out_method = heuristic_method
    if not out_dataset:
        out_dataset = heuristic_dataset
    return {
        "method_dependencies": out_method,
        "dataset_dependencies": out_dataset,
    }


def infer_dependency_titles(
    source_paper: dict,
    max_method: int = 3,
    max_dataset: int = 5,
) -> dict[str, list[dict]]:
    if not has_openai_key():
        return {"method_dependencies": [], "dataset_dependencies": []}

    system_prompt = (
        "You infer likely prior paper titles relied on by a source ML paper from title/abstract cues. "
        "Prioritize direct method lineage and explicit evaluation datasets/benchmarks. "
        "Return strict JSON."
    )
    user_prompt = json.dumps(
        {
            "task": "Infer likely prior paper titles that this paper depends on.",
            "source_paper": {
                "title": source_paper.get("title"),
                "abstract": (source_paper.get("abstract") or "")[:2200],
                "venue": source_paper.get("venue"),
                "year": source_paper.get("year"),
            },
            "output_schema": {
                "method_dependencies": [
                    {
                        "title": "paper title",
                        "role": "foundational_method | direct_technical_dependency",
                        "confidence": 0.0,
                        "reason": "short reason",
                    }
                ],
                "dataset_dependencies": [
                    {
                        "title": "paper or dataset benchmark title",
                        "role": "dataset_or_benchmark",
                        "confidence": 0.0,
                        "reason": "short reason",
                    }
                ],
            },
            "constraints": {
                "max_method": max_method,
                "max_dataset": max_dataset,
                "prefer_known_canonical_titles": True,
            },
        }
    )
    try:
        parsed = _chat_json(system_prompt, user_prompt, max_tokens=900)
    except Exception:
        return {"method_dependencies": [], "dataset_dependencies": []}

    out_method: list[dict] = []
    out_dataset: list[dict] = []
    raw_method = parsed.get("method_dependencies")
    if isinstance(raw_method, list):
        for dep in raw_method:
            if not isinstance(dep, dict):
                continue
            title = str(dep.get("title") or "").strip()
            if not title:
                continue
            role_raw = str(dep.get("role") or "direct_technical_dependency").lower()
            role = "foundational_method" if "foundational" in role_raw else "direct_technical_dependency"
            confidence = _clamp(_safe_float(dep.get("confidence"), 0.64))
            out_method.append(
                {
                    "title": title,
                    "role": role,
                    "confidence": confidence,
                    "reason": str(dep.get("reason") or "Inferred from introduction/problem and method cues."),
                }
            )
            if len(out_method) >= max_method:
                break

    raw_dataset = parsed.get("dataset_dependencies")
    if isinstance(raw_dataset, list):
        for dep in raw_dataset:
            if not isinstance(dep, dict):
                continue
            title = str(dep.get("title") or "").strip()
            if not title:
                continue
            confidence = _clamp(_safe_float(dep.get("confidence"), 0.58))
            out_dataset.append(
                {
                    "title": title,
                    "role": "dataset_or_benchmark",
                    "confidence": confidence,
                    "reason": str(dep.get("reason") or "Inferred from evaluation setup and benchmark cues."),
                }
            )
            if len(out_dataset) >= max_dataset:
                break

    return {
        "method_dependencies": out_method,
        "dataset_dependencies": out_dataset,
    }


def _heuristic_edge_explanation(
    source_paper: dict,
    target_paper: dict,
    relation_type: str,
    base_reason: str = "",
) -> str:
    source_title = (source_paper.get("title") or "This paper").strip()
    target_title = (target_paper.get("title") or "the prior work").strip()
    source_text = f"{source_title} {(source_paper.get('abstract') or '')}".lower()
    target_text = f"{target_title} {(target_paper.get('abstract') or '')}".lower()
    combined = f"{source_text} {target_text}"

    if any(x in combined for x in {"backbone", "encoder", "decoder", "architecture"}):
        link = "reuses a core model component"
    elif any(x in combined for x in {"objective", "loss", "training"}):
        link = "adopts the training objective or optimization design"
    elif any(x in combined for x in {"combine", "fusion", "hybrid"}):
        link = "combines ideas from this prior method"
    else:
        link = "extends this prior method as a direct technical dependency"

    base = str(base_reason or "").strip()
    relation = relation_type.replace("_", " ") if relation_type else "technical dependency"
    if base:
        return f"{source_title} {link} from {target_title} ({relation}). {base}"
    return f"{source_title} {link} from {target_title} ({relation})."


def explain_trace_edge(
    source_paper: dict,
    target_paper: dict,
    relation_type: str,
    base_reason: str = "",
) -> str:
    fallback = _heuristic_edge_explanation(source_paper, target_paper, relation_type, base_reason)
    if not has_openai_key():
        return fallback

    system_prompt = (
        "You explain methodological links between two papers in one concise sentence. "
        "Focus on mechanism: module reuse, objective inheritance, architecture adaptation, or method combination. "
        "Return strict JSON."
    )
    user_prompt = json.dumps(
        {
            "task": "Explain how the source paper is linked to the target prior paper.",
            "source_paper": {
                "title": source_paper.get("title"),
                "abstract": (source_paper.get("abstract") or "")[:1100],
                "venue": source_paper.get("venue"),
                "year": source_paper.get("year"),
            },
            "target_paper": {
                "title": target_paper.get("title"),
                "abstract": (target_paper.get("abstract") or "")[:1100],
                "venue": target_paper.get("venue"),
                "year": target_paper.get("year"),
            },
            "relation_type_hint": relation_type,
            "base_reason_hint": base_reason,
            "output_schema": {
                "explanation": "Single concise sentence under 30 words."
            },
        }
    )

    try:
        parsed = _chat_json(system_prompt, user_prompt, max_tokens=160)
    except Exception:
        return fallback

    explanation = str(parsed.get("explanation") or "").strip()
    if not explanation:
        return fallback
    if len(explanation) > 220:
        explanation = explanation[:220].rstrip() + "..."
    return explanation
