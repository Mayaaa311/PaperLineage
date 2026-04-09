from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PaperSearchRequest(BaseModel):
    topic: str = Field(default="", min_length=0)
    paper_name: str | None = None
    search_mode: Literal["topic", "paper_name"] = "topic"
    conferences: list[str] = Field(default_factory=list)
    start_year: int | None = None
    end_year: int | None = None
    user_id: str = "demo-user"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=10)
    max_results: int = Field(default=300, ge=10, le=300)
    use_saved_search: bool = True


class PaperOut(BaseModel):
    id: str
    external_id: str | None = None
    title: str
    authors: list[str]
    venue: str | None = None
    year: int | None = None
    abstract_snippet: str
    citation_count: int = 0
    review_score_avg: float | None = None
    review_count: int = 0
    decision: str | None = None
    url: str | None = None
    is_favorited: bool = False


class PaperSearchResponse(BaseModel):
    papers: list[PaperOut]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool
    source: str = "fresh"
    all_paper_ids: list[str] = Field(default_factory=list)


class SaveSearchRequest(BaseModel):
    topic: str = Field(default="", min_length=0)
    paper_name: str | None = None
    search_mode: Literal["topic", "paper_name"] = "topic"
    conferences: list[str] = Field(default_factory=list)
    start_year: int | None = None
    end_year: int | None = None
    user_id: str = "demo-user"
    max_results: int = Field(default=300, ge=10, le=300)
    paper_ids: list[str] = Field(default_factory=list)


class SaveSearchResponse(BaseModel):
    success: bool
    saved_count: int = 0


class FavoriteRequest(BaseModel):
    user_id: str = "demo-user"
    paper_id: str


class FavoriteResponse(BaseModel):
    success: bool


class PaperReferenceOut(BaseModel):
    id: str
    title: str
    year: int | None = None
    venue: str | None = None
    citation_count: int = 0
    url: str | None = None


class KeyDependencyOut(PaperReferenceOut):
    role: str
    confidence: float
    reason: str


class DatasetDependencyOut(PaperReferenceOut):
    role: str
    confidence: float
    reason: str


class PaperDetailResponse(BaseModel):
    id: str
    external_id: str | None = None
    title: str
    abstract: str | None = None
    authors: list[str]
    venue: str | None = None
    year: int | None = None
    citation_count: int = 0
    review_score_avg: float | None = None
    review_count: int = 0
    decision: str | None = None
    url: str | None = None
    is_favorited: bool = False
    references_count: int = 0
    references_preview: list[PaperReferenceOut] = Field(default_factory=list)
    quick_takeaways: list[str] = Field(default_factory=list)
    logic_summary: str = ""
    evidence_points: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    analysis_model: str | None = None
    key_dependencies: list[KeyDependencyOut] = Field(default_factory=list)
    dataset_dependencies: list[DatasetDependencyOut] = Field(default_factory=list)


class TraceStartRequest(BaseModel):
    user_id: str = "demo-user"
    paper_id: str
    trace_depth: int = Field(default=2, ge=1, le=6)


class TraceStartResponse(BaseModel):
    trace_id: int
    status: str


class TraceNodeOut(BaseModel):
    paper_id: str
    level: int
    title: str
    venue: str | None = None
    year: int | None = None
    citation_count: int = 0


class TraceEdgeOut(BaseModel):
    source_paper_id: str
    target_paper_id: str
    relation_type: str
    confidence: float
    reason: str


class TraceStatusResponse(BaseModel):
    trace_id: int
    status: str
    trace_depth: int
    created_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None
    root_paper_id: str
    nodes: list[TraceNodeOut] = Field(default_factory=list)
    edges: list[TraceEdgeOut] = Field(default_factory=list)


class TraceLatestResponse(BaseModel):
    found: bool
    trace: TraceStatusResponse | None = None


class FavoritesLinksGraphRequest(BaseModel):
    user_id: str = "demo-user"
    paper_ids: list[str] = Field(default_factory=list)
    max_related_edges: int = Field(default=36, ge=0, le=200)


class FavoritesLinksNodeOut(BaseModel):
    paper_id: str
    title: str
    venue: str | None = None
    year: int | None = None
    citation_count: int = 0
    level: int = 0
    is_selected_root: bool = False


class FavoritesLinksEdgeOut(BaseModel):
    source_paper_id: str
    target_paper_id: str
    relation_type: str
    confidence: float
    reason: str
    inferred: bool = False


class FavoritesLinksGraphResponse(BaseModel):
    selected_paper_ids: list[str] = Field(default_factory=list)
    nodes: list[FavoritesLinksNodeOut] = Field(default_factory=list)
    edges: list[FavoritesLinksEdgeOut] = Field(default_factory=list)
