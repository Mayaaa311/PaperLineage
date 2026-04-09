from __future__ import annotations

import html as html_lib
import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
_HTML_CACHE: dict[str, str] = {}
_OPENREVIEW_META_CACHE: dict[str, dict] = {}
_OPENREVIEW_FETCH_COUNT = 0
_OPENREVIEW_FETCH_LIMIT = 80
DBLP_SLUGS: dict[str, str] = {
    "ICLR": "iclr",
    "ICML": "icml",
    "KDD": "kdd",
}


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _matches_query(title: str, query: str, search_mode: str) -> bool:
    title_l = title.lower().strip()
    query_l = query.lower().strip()
    if not query_l:
        return True
    if search_mode == "paper_name":
        return query_l in title_l or title_l in query_l

    q_tokens = [t for t in _tokenize(query_l) if len(t) > 2]
    if not q_tokens:
        return query_l in title_l
    matched = sum(1 for t in q_tokens if t in title_l)
    threshold = 1 if len(q_tokens) <= 2 else max(2, len(q_tokens) // 2)
    return matched >= threshold


def _make_paper(
    venue: str,
    year: int,
    title: str,
    url: str,
    review_score_avg: float | None = None,
    review_count: int = 0,
    decision: str | None = None,
) -> dict:
    key = f"{venue}|{year}|{title}|{url}"
    external_id = "SCRAPE:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return {
        "external_id": external_id,
        "title": title,
        "abstract": None,
        "year": year,
        "venue": venue,
        "authors": [],
        "citation_count": 0,
        "review_score_avg": review_score_avg,
        "review_count": review_count,
        "decision": decision,
        "url": url,
        "references": [],
    }


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw or "")
    return html_lib.unescape(text).strip()


def _fetch_html(url: str) -> str:
    cached = _HTML_CACHE.get(url)
    if cached is not None:
        return cached

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    with httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        _HTML_CACHE[url] = response.text
        return response.text


def _coerce_content_value(value):
    if isinstance(value, dict):
        if "value" in value:
            return value.get("value")
        if "choices" in value and isinstance(value.get("choices"), list):
            choices = value.get("choices") or []
            return choices[0] if choices else None
    return value


def _extract_numeric_rating(value) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _extract_openreview_forum_id(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.netloc or "").lower()
    if "openreview.net" not in host:
        return None
    query = parse_qs(parsed.query or "")
    rid = (query.get("id") or [None])[0]
    if rid:
        return rid
    path = parsed.path or ""
    if "/pdf/" in path:
        token = path.rstrip("/").split("/")[-1]
        if token:
            return token
    return None


def _iter_openreview_notes(payload: dict) -> list[dict]:
    notes = payload.get("notes")
    if not isinstance(notes, list):
        return []
    out: list[dict] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        out.append(note)
        details = note.get("details")
        if isinstance(details, dict):
            direct_replies = details.get("directReplies") or details.get("direct_replies")
            if isinstance(direct_replies, list):
                for rep in direct_replies:
                    if isinstance(rep, dict):
                        out.append(rep)
    return out


def _parse_openreview_metadata(payload: dict) -> dict:
    notes = _iter_openreview_notes(payload)
    if not notes:
        return {}

    ratings: list[float] = []
    best_decision: str | None = None

    for note in notes:
        content = note.get("content")
        if not isinstance(content, dict):
            continue
        invitation = str(note.get("invitation") or "").lower()

        # Ratings from review/meta-review notes.
        for key in ("rating", "overall_rating", "recommendation", "score"):
            raw = _coerce_content_value(content.get(key))
            val = _extract_numeric_rating(raw)
            if val is not None:
                ratings.append(val)
                break

        # Decision field.
        decision_raw = (
            _coerce_content_value(content.get("decision"))
            or _coerce_content_value(content.get("final_decision"))
            or _coerce_content_value(content.get("venue"))
        )
        if decision_raw and ("decision" in invitation or "decision" in str(decision_raw).lower()):
            best_decision = str(decision_raw).strip()
        elif decision_raw and not best_decision and (
            "accept" in str(decision_raw).lower()
            or "reject" in str(decision_raw).lower()
            or "poster" in str(decision_raw).lower()
            or "oral" in str(decision_raw).lower()
            or "spotlight" in str(decision_raw).lower()
        ):
            best_decision = str(decision_raw).strip()

    out = {}
    if ratings:
        out["review_score_avg"] = round(sum(ratings) / len(ratings), 3)
        out["review_count"] = len(ratings)
    if best_decision:
        out["decision"] = best_decision
    return out


def _fetch_openreview_metadata(forum_id: str) -> dict:
    global _OPENREVIEW_FETCH_COUNT
    cached = _OPENREVIEW_META_CACHE.get(forum_id)
    if cached is not None:
        return cached
    if _OPENREVIEW_FETCH_COUNT >= _OPENREVIEW_FETCH_LIMIT:
        _OPENREVIEW_META_CACHE[forum_id] = {}
        return {}
    _OPENREVIEW_FETCH_COUNT += 1
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
    }
    candidates = [
        f"https://api2.openreview.net/notes?forum={forum_id}&limit=1000&details=directReplies",
        f"https://api.openreview.net/notes?forum={forum_id}&limit=1000&details=directReplies",
    ]
    parsed: dict = {}
    with httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True) as client:
        for url in candidates:
            try:
                res = client.get(url, headers=headers)
                if res.status_code != 200:
                    continue
                body = res.json()
                parsed = _parse_openreview_metadata(body)
                if parsed:
                    break
            except Exception:
                continue
    _OPENREVIEW_META_CACHE[forum_id] = parsed
    return parsed


def _attach_openreview_metadata(paper: dict) -> dict:
    forum_id = _extract_openreview_forum_id(paper.get("url"))
    if not forum_id:
        return paper
    meta = _fetch_openreview_metadata(forum_id)
    if not meta:
        return paper
    if meta.get("review_score_avg") is not None:
        paper["review_score_avg"] = meta.get("review_score_avg")
    if meta.get("review_count") is not None:
        paper["review_count"] = int(meta.get("review_count") or 0)
    if meta.get("decision"):
        paper["decision"] = meta.get("decision")
    return paper


def _scrape_dblp_conference(
    conf: str,
    year: int,
    query: str,
    search_mode: str,
    max_results: int,
) -> list[dict]:
    slug = DBLP_SLUGS.get(conf.upper())
    if not slug:
        return []

    url = f"https://dblp.org/db/conf/{slug}/{slug}{year}.html"
    html = _fetch_html(url)
    entry_starts = list(re.finditer(r'<li class="entry inproceedings', html))
    if not entry_starts:
        return []

    papers: list[dict] = []
    seen_titles: set[str] = set()

    for idx, start_match in enumerate(entry_starts):
        start_idx = start_match.start()
        end_idx = entry_starts[idx + 1].start() if idx + 1 < len(entry_starts) else len(html)
        block = html[start_idx:end_idx]

        title_match = re.search(r'<span class="title" itemprop="name">(.*?)</span>', block, flags=re.S)
        if not title_match:
            continue
        title = _strip_html(title_match.group(1))
        if not title:
            continue
        if title.lower().startswith("proceedings of "):
            continue
        if not _matches_query(title, query, search_mode):
            continue

        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        ee_match = re.search(r'<li class="ee"><a href="([^"]+)" itemprop="url"', block)
        if ee_match:
            link = html_lib.unescape(ee_match.group(1))
        else:
            head_match = re.search(r'<div class="head"><a href="([^"]+)"', block)
            link = html_lib.unescape(head_match.group(1)) if head_match else url

        papers.append(_attach_openreview_metadata(_make_paper(conf, year, title, link)))
        if len(papers) >= max_results:
            break

    return papers


def _scrape_neurips(year: int, query: str, search_mode: str, max_results: int) -> list[dict]:
    url = f"https://proceedings.neurips.cc/paper_files/paper/{year}"
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    for a in soup.select(f'a[href^="/paper_files/paper/{year}/hash/"][href$="-Abstract-Conference.html"]'):
        title = a.get_text(" ", strip=True)
        if not title or not _matches_query(title, query, search_mode):
            continue
        link = urljoin("https://proceedings.neurips.cc", a.get("href", ""))
        papers.append(_attach_openreview_metadata(_make_paper("NeurIPS", year, title, link)))
        if len(papers) >= max_results:
            break
    return papers


def _scrape_cvpr(year: int, query: str, search_mode: str, max_results: int) -> list[dict]:
    url = f"https://openaccess.thecvf.com/CVPR{year}?day=all"
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    for a in soup.select("dt.ptitle a"):
        title = a.get_text(" ", strip=True)
        if not title or not _matches_query(title, query, search_mode):
            continue
        link = urljoin("https://openaccess.thecvf.com", a.get("href", ""))
        papers.append(_attach_openreview_metadata(_make_paper("CVPR", year, title, link)))
        if len(papers) >= max_results:
            break
    return papers


def _scrape_eccv(year: int, query: str, search_mode: str, max_results: int) -> list[dict]:
    url = "https://www.ecva.net/papers.php"
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    prefix = f"papers/eccv_{year}/papers_ECCV/html/"
    papers: list[dict] = []
    seen_titles: set[str] = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if prefix not in href:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        if title.lower() in {"pdf", "supplementary material"}:
            continue
        if not _matches_query(title, query, search_mode):
            continue
        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        link = urljoin("https://www.ecva.net/", href)
        papers.append(_attach_openreview_metadata(_make_paper("ECCV", year, title, link)))
        if len(papers) >= max_results:
            break
    return papers


def _scrape_acl_family(
    conf: str,
    slug: str,
    year: int,
    query: str,
    search_mode: str,
    max_results: int,
) -> list[dict]:
    url = f"https://aclanthology.org/events/{slug}-{year}/"
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    papers: list[dict] = []
    seen: set[str] = set()
    prefix = f"/{year}.{slug}"
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href.startswith(prefix):
            continue
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 8:
            continue
        if title.lower().startswith("proceedings of"):
            continue
        if not _matches_query(title, query, search_mode):
            continue
        key = f"{href}|{title}".lower()
        if key in seen:
            continue
        seen.add(key)
        link = urljoin("https://aclanthology.org", href)
        papers.append(_attach_openreview_metadata(_make_paper(conf, year, title, link)))
        if len(papers) >= max_results:
            break
    return papers


def scrape_conference_websites(
    query: str,
    conferences: list[str],
    start_year: int | None,
    end_year: int | None,
    search_mode: str,
    max_results: int = 300,
) -> list[dict]:
    if not conferences:
        return []

    year_start = start_year if start_year is not None else 2020
    year_end = end_year if end_year is not None else 2026
    years = [y for y in range(year_start, year_end + 1) if 1990 <= y <= 2030]
    if not years:
        return []

    results: list[dict] = []
    seen_titles: set[str] = set()

    for conf in conferences:
        conf_u = conf.upper()
        for year in years:
            try:
                if conf_u == "NEURIPS":
                    batch = _scrape_neurips(year, query, search_mode, max_results=max_results)
                elif conf_u == "CVPR":
                    batch = _scrape_cvpr(year, query, search_mode, max_results=max_results)
                elif conf_u == "ECCV":
                    batch = _scrape_eccv(year, query, search_mode, max_results=max_results)
                elif conf_u == "ACL":
                    batch = _scrape_acl_family(
                        "ACL", "acl", year, query, search_mode, max_results=max_results
                    )
                elif conf_u == "EMNLP":
                    batch = _scrape_acl_family(
                        "EMNLP", "emnlp", year, query, search_mode, max_results=max_results
                    )
                else:
                    batch = _scrape_dblp_conference(
                        conf_u, year, query, search_mode, max_results=max_results
                    )
            except Exception:
                batch = []

            for item in batch:
                title_key = (item.get("title") or "").strip().lower()
                if not title_key or title_key in seen_titles:
                    continue
                seen_titles.add(title_key)
                results.append(item)
                if len(results) >= max_results:
                    return results
    return results
