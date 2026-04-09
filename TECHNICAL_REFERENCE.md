# Technical Reference

## Webapp Flow (Visual)

```mermaid
flowchart LR
  A["1) Search Setup<br/>Topic or Paper Name<br/>Conference + Years"] --> B["2) Paper Discovery<br/>Ranked by citation count"]
  B --> C["3) Favorites<br/>Persist selected papers"]
  B --> D["4) Paper Detail<br/>Summary + dependencies"]
  C --> D
  D --> E["5) Trace-Back<br/>Recursive lineage graph"]
  C --> F["Visualize Links Button"]
  F --> G["Select favorite papers"]
  G --> H["/favorites-links page<br/>Merged graph + detail panel"]
  H --> D
```

---

## Interface Map

```mermaid
flowchart TB
  subgraph MainPage["Main Page (/)"]
    S["Search Setup"]
    R["Paper Discovery"]
    V["Favorites"]
    P["Paper Detail"]
    G["Method Lineage Graph"]
  end

  subgraph LinksPage["Favorites Links Page (/favorites-links)"]
    F1["Selected Favorites Meta"]
    F2["Paper Detail Panel"]
    F3["Combined Favorite Links Graph"]
  end

  V -->|Visualize Links| F1
  F3 -->|Node click| F2
```

---

## Core Functionalities (with diagrams)

### 1) Search (Topic mode vs Paper-name mode)

```mermaid
flowchart TD
  Q["Search Request"] --> M{"search_mode"}
  M -->|topic| T1["Scrape official conference websites"]
  M -->|paper_name| P1["Query Semantic Scholar + OpenAlex"]

  T1 --> T2["Local DB fallback"]
  P1 --> P2["Strict local filters<br/>(title, year, conference)"]
  P2 --> P3{Any results?}
  P3 -->|No| P4["Conference scrape fallback"]
  P3 -->|Yes| R
  P4 --> R
  T2 --> R

  R["Deduplicate + enrich citations + rank by citations"] --> C["Auto-save search snapshot for pagination cache"]
```

What you get:
- Pagination-friendly results (10 per page)
- Saved-search reuse to avoid re-scraping/re-querying on page flips

### 2) Paper Detail + Analysis

```mermaid
flowchart TD
  O["Open paper detail"] --> C{"prefer_cached?"}
  C -->|true| A1["Read cached detail + cached analysis"]
  C -->|false| A2["Enrich from external providers if needed"]
  A2 --> A3["Cache references preview"]
  A1 --> A4["Return structured detail payload"]
  A3 --> A4

  A4 --> U["UI panels: quick read, logic/evidence, limitations, key deps, datasets"]
```

Analysis behavior:
- Uses LLM when `OPENAI_API_KEY` is set
- Falls back to heuristics if LLM unavailable
- Limitations are inferred from discussion/conclusion context when available

### 3) Recursive Trace-Back

```mermaid
flowchart TD
  T["Start trace request"] --> E{"Existing completed trace for same root+depth?"}
  E -->|Yes and valid| R1["Return cached trace"]
  E -->|No / stale| B["Background trace job"]

  B --> W["Walk references recursively up to trace_depth"]
  W --> K["Select top method dependencies"]
  K --> G["Persist trace nodes + edges"]
  G --> R2["Trace status API returns graph"]
```

Graph semantics:
- Node click: opens paper detail
- Edge click: concise reason text
- Edge color: relation confidence tier
- Node color intensity: incoming-edge count (cited-by count **inside graph**)

### 4) Favorites Links Graph (cross-paper merge)

```mermaid
flowchart TD
  F["Click Visualize Links"] --> S["Select favorite papers"]
  S --> X["/favorites-links page"]
  X --> A["Fetch latest completed traces for selected roots"]
  A --> M["Merge all nodes + edges"]
  M --> L["Add inferred related-topic links when similarity is high"]
  L --> U["Render combined graph"]
  U --> D["Node click opens detail panel on same page"]
```

Important behavior:
- If selected papers are unrelated, they remain separate components
- Related components get inferred links

---

## Architecture

```mermaid
flowchart LR
  subgraph Frontend
    UI1["static/index.html + app.js"]
    UI2["static/favorites_links.html + favorites_links.js"]
    CY["Cytoscape.js"]
  end

  subgraph Backend["FastAPI"]
    API["app/main.py routes"]
    TRACE["app/trace.py"]
    ANALYSIS["app/paper_analysis.py + app/llm.py"]
    SCRAPE["app/conference_scraper.py"]
    SCHOLAR["app/scholar.py"]
  end

  subgraph Data
    DB["SQLite / SQLAlchemy"]
    LLMDB[(llm_cache.db)]
  end

  subgraph External
    OA["OpenAlex"]
    S2["Semantic Scholar"]
    CONF["Conference websites"]
    OR["OpenReview metadata"]
    GPT["OpenAI API"]
  end

  UI1 --> API
  UI2 --> API
  API --> TRACE
  API --> ANALYSIS
  API --> SCRAPE
  API --> SCHOLAR
  TRACE --> DB
  ANALYSIS --> DB
  API --> DB
  ANALYSIS --> LLMDB

  SCHOLAR --> OA
  SCHOLAR --> S2
  SCRAPE --> CONF
  SCRAPE --> OR
  ANALYSIS --> GPT
  CY --> UI1
  CY --> UI2
```

---

## Caching Strategy (why repeated opens are fast)

```mermaid
flowchart TD
  A["Search"] --> S1["SavedSearch + SavedSearchPaper"]
  B["Paper detail references"] --> S2["PaperDetailCache"]
  C["Paper structured analysis"] --> S3["PaperAnalysis"]
  D["Trace graph"] --> S4["TraceRequest + TraceGraphNode + TraceGraphEdge"]
  E["LLM prompt/response JSON"] --> S5["llm_cache.db"]
```

Practical impact:
- Paginating search does not re-scrape
- Reopening favorited papers uses cached summary/analysis
- Reopening trace graphs reuses stored nodes/edges

---

## Database Schema (ER)

```mermaid
erDiagram
  PAPER ||--o{ FAVORITE : is_saved_in
  PAPER ||--o| PAPER_ANALYSIS : has
  PAPER ||--o| PAPER_DETAIL_CACHE : has

  TRACE_REQUEST ||--o{ TRACE_GRAPH_NODE : contains
  TRACE_REQUEST ||--o{ TRACE_GRAPH_EDGE : contains
  PAPER ||--o{ TRACE_GRAPH_NODE : appears_as
  PAPER ||--o{ TRACE_GRAPH_EDGE : source_or_target

  SAVED_SEARCH ||--o{ SAVED_SEARCH_PAPER : includes
  PAPER ||--o{ SAVED_SEARCH_PAPER : referenced

  PAPER {
    string id PK
    string external_id
    string title
    text abstract
    string venue
    int year
    int citation_count
    float review_score_avg
    int review_count
    string decision
    string url
  }

  PAPER_ANALYSIS {
    int id PK
    string paper_id FK
    text quick_takeaways_json
    text logic_summary
    text evidence_points_json
    text limitations_json
    text key_dependencies_json
    text dataset_dependencies_json
    string model_name
  }

  PAPER_DETAIL_CACHE {
    int id PK
    string paper_id FK
    int references_count
    text references_preview_json
  }

  FAVORITE {
    int id PK
    string user_id
    string paper_id FK
    datetime created_at
  }

  TRACE_REQUEST {
    int id PK
    string user_id
    string root_paper_id FK
    int trace_depth
    int max_branching
    string status
    text error_message
  }

  TRACE_GRAPH_NODE {
    int id PK
    int trace_request_id FK
    string paper_id FK
    int level
  }

  TRACE_GRAPH_EDGE {
    int id PK
    int trace_request_id FK
    string source_paper_id FK
    string target_paper_id FK
    string relation_type
    float confidence
    string reason
  }

  SAVED_SEARCH {
    int id PK
    string user_id
    string search_key
    string search_mode
    string query_text
    text conferences_json
    int start_year
    int end_year
    int max_results
  }

  SAVED_SEARCH_PAPER {
    int id PK
    int saved_search_id FK
    string paper_id FK
    int rank
  }
```

---

## API Surface

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/` | Main web app page |
| `GET` | `/favorites-links` | Dedicated merged-favorites graph page |
| `POST` | `/api/papers/search` | Search papers |
| `GET` | `/api/papers/{paper_id}` | Get paper detail + analysis |
| `POST` | `/api/searches/save` | Save current search snapshot |
| `POST` | `/api/favorites` | Add favorite |
| `DELETE` | `/api/favorites/{paper_id}?user_id=...` | Remove favorite |
| `GET` | `/api/favorites?user_id=...` | List favorites |
| `POST` | `/api/favorites/links-graph` | Build merged graph from selected favorites |
| `POST` | `/api/traces` | Start trace-back job |
| `GET` | `/api/traces/{trace_id}` | Poll trace status/result |
| `GET` | `/api/traces/by-paper/latest?paper_id=...&user_id=...` | Load latest cached trace for a paper |

---

