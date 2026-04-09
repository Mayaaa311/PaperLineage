from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from .db import Base


class Paper(Base):
    __tablename__ = "papers"

    id = Column(String, primary_key=True)
    external_id = Column(String, unique=True, nullable=True, index=True)
    title = Column(String, index=True, nullable=False)
    abstract = Column(Text, nullable=True)
    venue = Column(String, nullable=True, index=True)
    year = Column(Integer, nullable=True, index=True)
    authors_json = Column(Text, default="[]", nullable=False)
    citation_count = Column(Integer, default=0, nullable=False)
    review_score_avg = Column(Float, nullable=True)
    review_count = Column(Integer, default=0, nullable=False)
    decision = Column(String, nullable=True)
    url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "paper_id", name="uq_favorite_user_paper"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaperAnalysis(Base):
    __tablename__ = "paper_analyses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    paper_id = Column(String, ForeignKey("papers.id"), unique=True, index=True, nullable=False)
    quick_takeaways_json = Column(Text, default="[]", nullable=False)
    logic_summary = Column(Text, default="", nullable=False)
    evidence_points_json = Column(Text, default="[]", nullable=False)
    limitations_json = Column(Text, default="[]", nullable=False)
    key_dependencies_json = Column(Text, default="[]", nullable=False)
    dataset_dependencies_json = Column(Text, default="[]", nullable=False)
    model_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TraceRequest(Base):
    __tablename__ = "trace_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    root_paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    trace_depth = Column(Integer, nullable=False)
    max_branching = Column(Integer, default=3, nullable=False)
    status = Column(String, default="pending", index=True, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class TraceGraphNode(Base):
    __tablename__ = "trace_graph_nodes"
    __table_args__ = (UniqueConstraint("trace_request_id", "paper_id", name="uq_trace_node"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_request_id = Column(Integer, ForeignKey("trace_requests.id"), index=True, nullable=False)
    paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    level = Column(Integer, index=True, nullable=False)


class TraceGraphEdge(Base):
    __tablename__ = "trace_graph_edges"
    __table_args__ = (
        UniqueConstraint("trace_request_id", "source_paper_id", "target_paper_id", name="uq_trace_edge"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trace_request_id = Column(Integer, ForeignKey("trace_requests.id"), index=True, nullable=False)
    source_paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    target_paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    relation_type = Column(String, default="relies_on", nullable=False)
    confidence = Column(Float, default=0.0, nullable=False)
    reason = Column(String, default="", nullable=False)


class SavedSearch(Base):
    __tablename__ = "saved_searches"
    __table_args__ = (UniqueConstraint("user_id", "search_key", name="uq_saved_search_user_key"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, index=True, nullable=False)
    search_key = Column(String, index=True, nullable=False)
    search_mode = Column(String, nullable=False)
    query_text = Column(String, nullable=False)
    conferences_json = Column(Text, default="[]", nullable=False)
    start_year = Column(Integer, nullable=True)
    end_year = Column(Integer, nullable=True)
    max_results = Column(Integer, default=300, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class SavedSearchPaper(Base):
    __tablename__ = "saved_search_papers"
    __table_args__ = (
        UniqueConstraint("saved_search_id", "paper_id", name="uq_saved_search_paper"),
        UniqueConstraint("saved_search_id", "rank", name="uq_saved_search_rank"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    saved_search_id = Column(Integer, ForeignKey("saved_searches.id"), index=True, nullable=False)
    paper_id = Column(String, ForeignKey("papers.id"), index=True, nullable=False)
    rank = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PaperDetailCache(Base):
    __tablename__ = "paper_detail_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    paper_id = Column(String, ForeignKey("papers.id"), unique=True, index=True, nullable=False)
    references_count = Column(Integer, default=0, nullable=False)
    references_preview_json = Column(Text, default="[]", nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
