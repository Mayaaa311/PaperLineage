# Paper Lineage Explorer (MVP)

Web app for:

1. Topic or paper-name + conference + year paper discovery.
2. Paginated discovery results (up to top 300, 10 per page).
3. Persistent favorites in a database.
4. Paper detail view with recursive trace-back depth.
5. Method-lineage graph visualization.
6. LLM-generated paper analysis (problem/gap/method + logic/evidence).
7. LLM-selected key dependencies (no manual branching control).

## Stack

- Backend: FastAPI + SQLAlchemy
- Database: SQLite (default, local file)
- Data source: Semantic Scholar Graph API
- Frontend: Vanilla JS + Cytoscape.js

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.

## Optional config

Set `SEMANTIC_SCHOLAR_API_KEY` to improve Semantic Scholar API limits:

```bash
export SEMANTIC_SCHOLAR_API_KEY=your_key_here
```

Set `OPENAI_API_KEY` to enable GPT-based paper analysis and dependency selection:

```bash
export OPENAI_API_KEY=your_key_here
```

Optional model override (cheap default is already `gpt-4o-mini`):

```bash
export OPENAI_MODEL=gpt-4o-mini
```

Set `DATABASE_URL` if you want a different DB:

```bash
export DATABASE_URL=sqlite:///./paper_reading.db
```

## API endpoints

- `POST /api/papers/search`
- `GET /api/papers/{paper_id}`
- `POST /api/favorites`
- `DELETE /api/favorites/{paper_id}?user_id=...`
- `GET /api/favorites?user_id=...`
- `POST /api/traces`
- `GET /api/traces/{trace_id}`
# PaperLineage
