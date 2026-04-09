import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Paper


def load_authors(authors_json: str | None) -> list[str]:
    if not authors_json:
        return []
    try:
        parsed = json.loads(authors_json)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except json.JSONDecodeError:
        return []
    return []


def load_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def load_json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def upsert_paper(db: Session, normalized: dict) -> Paper:
    external_id = normalized.get("external_id")
    title = normalized.get("title")
    if not title:
        raise ValueError("Cannot upsert paper without title.")

    paper = None
    if external_id:
        paper = db.execute(select(Paper).where(Paper.external_id == external_id)).scalar_one_or_none()

    if not paper:
        paper = Paper(
            id=str(uuid.uuid4()),
            external_id=external_id,
            title=title,
            abstract=normalized.get("abstract"),
            venue=normalized.get("venue"),
            year=normalized.get("year"),
            authors_json=json.dumps(normalized.get("authors", [])),
            citation_count=normalized.get("citation_count") or 0,
            url=normalized.get("url"),
        )
        db.add(paper)
        db.flush()
        return paper

    paper.title = normalized.get("title") or paper.title
    paper.abstract = normalized.get("abstract") or paper.abstract
    paper.venue = normalized.get("venue") or paper.venue
    paper.year = normalized.get("year") or paper.year
    if normalized.get("authors"):
        paper.authors_json = json.dumps(normalized["authors"])
    paper.citation_count = max(paper.citation_count, normalized.get("citation_count") or 0)
    paper.url = normalized.get("url") or paper.url
    db.flush()
    return paper


def paper_to_output(paper: Paper, is_favorited: bool = False) -> dict:
    abstract = paper.abstract or ""
    snippet = abstract[:220] + ("..." if len(abstract) > 220 else "")
    return {
        "id": paper.id,
        "external_id": paper.external_id,
        "title": paper.title,
        "authors": load_authors(paper.authors_json),
        "venue": paper.venue,
        "year": paper.year,
        "abstract_snippet": snippet,
        "citation_count": paper.citation_count or 0,
        "url": paper.url,
        "is_favorited": is_favorited,
    }
