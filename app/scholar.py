import os
from urllib.parse import quote

import httpx

BASE_URL = "https://api.semanticscholar.org/graph/v1"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
OPENALEX_CONFERENCE_SOURCE_IDS = {
    "ICLR": ["https://openalex.org/S4306419637"],
    "NEURIPS": ["https://openalex.org/S4393916742", "https://openalex.org/S4306420609"],
    "ICML": ["https://openalex.org/S4306419644"],
    "CVPR": ["https://openalex.org/S4363607701", "https://openalex.org/S4210176548"],
    "ECCV": ["https://openalex.org/S4306418318"],
    "ACL": ["https://openalex.org/S4363608652"],
    "EMNLP": ["https://openalex.org/S4306418267", "https://openalex.org/S4363608991"],
    "KDD": ["https://openalex.org/S4306420424"],
}

SEARCH_FIELDS = ",".join(
    [
        "paperId",
        "title",
        "abstract",
        "year",
        "venue",
        "citationCount",
        "url",
        "authors",
    ]
)

DETAIL_FIELDS = ",".join(
    [
        "paperId",
        "title",
        "abstract",
        "year",
        "venue",
        "citationCount",
        "url",
        "authors",
        "references.paperId",
        "references.title",
        "references.abstract",
        "references.year",
        "references.venue",
        "references.citationCount",
        "references.url",
        "references.authors",
    ]
)

OPENALEX_SELECT_BASE = ",".join(
    [
        "id",
        "display_name",
        "abstract_inverted_index",
        "publication_year",
        "primary_location",
        "cited_by_count",
        "authorships",
        "doi",
    ]
)
OPENALEX_SELECT_DETAIL = OPENALEX_SELECT_BASE + ",referenced_works"


class ScholarClient:
    def __init__(self):
        self.api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY")
        self.timeout = httpx.Timeout(timeout=20.0)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    def search_papers(
        self,
        query: str,
        limit: int = 50,
        conferences: list[str] | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> list[dict]:
        semantic_results: list[dict] = []
        try:
            semantic_results = self._search_semantic_scholar(query=query, limit=limit)
        except Exception:
            semantic_results = []

        openalex_results: list[dict] = []
        try:
            # Query OpenAlex as a companion source for better coverage under strict filters.
            openalex_results = self._search_openalex(
                query=query,
                limit=limit,
                conferences=conferences,
                start_year=start_year,
                end_year=end_year,
            )
        except Exception:
            openalex_results = []

        if not semantic_results:
            return openalex_results
        if not openalex_results:
            return semantic_results

        merged: list[dict] = []
        seen_ids: set[str] = set()
        for item in semantic_results + openalex_results:
            pid = str(item.get("paperId") or "")
            title = str(item.get("title") or "").strip().lower()
            dedupe_key = pid or title
            if not dedupe_key:
                continue
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            merged.append(item)
            if len(merged) >= limit:
                break
        return merged

    def _search_semantic_scholar(self, query: str, limit: int = 50) -> list[dict]:
        params = {
            "query": query,
            "limit": limit,
            "fields": SEARCH_FIELDS,
        }
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{BASE_URL}/paper/search", params=params, headers=self._headers())
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])

    def _search_openalex(
        self,
        query: str,
        limit: int = 50,
        conferences: list[str] | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> list[dict]:
        base_params = {
            "search": query,
            "per-page": max(1, min(200, limit)),
            "select": OPENALEX_SELECT_BASE,
        }

        year_filter: str | None = None
        if start_year is not None and end_year is not None:
            year_filter = f"publication_year:{start_year}-{end_year}"
        elif start_year is not None:
            year_filter = f"from_publication_date:{start_year}-01-01"
        elif end_year is not None:
            year_filter = f"to_publication_date:{end_year}-12-31"

        source_ids: list[str] = []
        for conf in conferences or []:
            source_ids.extend(OPENALEX_CONFERENCE_SOURCE_IDS.get(conf.upper(), []))

        with httpx.Client(timeout=self.timeout) as client:
            merged: list[dict] = []
            seen_ids: set[str] = set()

            # First pass: conference source constrained (high precision, lower recall).
            if source_ids:
                filtered_params = dict(base_params)
                filter_parts = [
                    "primary_location.source.id:" + "|".join(sorted(set(source_ids)))
                ]
                if year_filter:
                    filter_parts.append(year_filter)
                filtered_params["filter"] = ",".join(filter_parts)
                response = client.get(OPENALEX_WORKS_URL, params=filtered_params)
                response.raise_for_status()
                for work in response.json().get("results", []):
                    paper = _openalex_to_semantic(work)
                    pid = paper.get("paperId")
                    if not pid or pid in seen_ids:
                        continue
                    seen_ids.add(pid)
                    merged.append(paper)
                    if len(merged) >= limit:
                        return merged

            # Second pass: unconstrained search for recall; local filtering will refine later.
            unfiltered_params = dict(base_params)
            if year_filter:
                unfiltered_params["filter"] = year_filter
            response = client.get(OPENALEX_WORKS_URL, params=unfiltered_params)
            response.raise_for_status()
            for work in response.json().get("results", []):
                paper = _openalex_to_semantic(work)
                pid = paper.get("paperId")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                merged.append(paper)
                if len(merged) >= limit:
                    break
            return merged

    def get_paper(self, external_id: str) -> dict | None:
        if not external_id:
            return None

        if external_id.startswith("OA:") or external_id.startswith("https://openalex.org/"):
            return self._get_openalex_paper(external_id)

        safe_id = quote(external_id, safe="")
        params = {"fields": DETAIL_FIELDS}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{BASE_URL}/paper/{safe_id}", params=params, headers=self._headers())
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()

    def _get_openalex_paper(self, external_id: str) -> dict | None:
        openalex_id = _extract_openalex_id(external_id)
        if not openalex_id:
            return None

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(
                f"{OPENALEX_WORKS_URL}/{openalex_id}",
                params={"select": OPENALEX_SELECT_DETAIL},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            root_work = response.json()
            root_paper = _openalex_to_semantic(root_work)

            raw_ref_ids = root_work.get("referenced_works", []) or []
            ref_ids = [_extract_openalex_id(x) for x in raw_ref_ids[:60]]
            ref_ids = [x for x in ref_ids if x]
            if not ref_ids:
                return root_paper

            refs_resp = client.get(
                OPENALEX_WORKS_URL,
                params={
                    "filter": "openalex:" + "|".join(ref_ids),
                    "per-page": len(ref_ids),
                    "select": OPENALEX_SELECT_BASE,
                },
            )
            refs_resp.raise_for_status()
            refs = refs_resp.json().get("results", [])
            root_paper["references"] = [_openalex_to_semantic(work) for work in refs]
            return root_paper


def normalize_paper(raw: dict) -> dict:
    authors = raw.get("authors") or []
    author_names = [a.get("name", "").strip() for a in authors if a.get("name")]
    return {
        "external_id": raw.get("paperId"),
        "title": (raw.get("title") or "").strip(),
        "abstract": raw.get("abstract"),
        "year": raw.get("year"),
        "venue": raw.get("venue"),
        "authors": author_names,
        "citation_count": raw.get("citationCount") or 0,
        "review_score_avg": raw.get("review_score_avg"),
        "review_count": raw.get("review_count") or 0,
        "decision": raw.get("decision"),
        "url": raw.get("url"),
        "references": raw.get("references") or [],
    }


def _extract_openalex_id(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("OA:"):
        return value.split(":", 1)[1]
    if "openalex.org/" in value:
        return value.rstrip("/").split("/")[-1]
    return value


def _deinvert_abstract(index: dict | None) -> str | None:
    if not index:
        return None
    max_pos = -1
    for positions in index.values():
        if not isinstance(positions, list):
            continue
        if positions:
            max_pos = max(max_pos, max(positions))
    if max_pos < 0:
        return None

    words = [""] * (max_pos + 1)
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            if isinstance(pos, int) and 0 <= pos <= max_pos:
                words[pos] = word
    text = " ".join(w for w in words if w).strip()
    return text or None


def _openalex_to_semantic(work: dict) -> dict:
    openalex_id = _extract_openalex_id(work.get("id"))
    paper_id = f"OA:{openalex_id}" if openalex_id else None
    authors = []
    for authorship in work.get("authorships", []) or []:
        name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if name:
            authors.append({"name": name})

    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    venue = source.get("display_name")
    url = primary_location.get("landing_page_url") or work.get("doi") or work.get("id")

    return {
        "paperId": paper_id,
        "title": (work.get("display_name") or "").strip(),
        "abstract": _deinvert_abstract(work.get("abstract_inverted_index")),
        "year": work.get("publication_year"),
        "venue": venue,
        "citationCount": work.get("cited_by_count") or 0,
        "url": url,
        "authors": authors,
        "references": [],
    }
