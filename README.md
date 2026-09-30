# LLM Wiki Agent

LLM Wiki Agent compiles immutable source material into a human-readable, Git-versioned
Markdown Wiki. It treats the Wiki as the primary knowledge asset and uses raw-document RAG
only as an explicit fallback.

## Design

```text
PDF / DOCX / Markdown / TXT / HTML
                  |
          immutable revisions
                  |
      CPU parser worker + spans
                  |
        LangGraph compilation
                  |
     Markdown + YAML + citations
                  |
    Git commit (knowledge source of truth)
                  |
       rebuildable projections
      /          |           \
   BM25       graph links     vector
      \          |           /
        RRF + optional reranker
                  |
       raw-source RAG fallback
```

Published knowledge lives in:

- `knowledge/wiki/sources/`
- `knowledge/wiki/entities/`
- `knowledge/wiki/concepts/`
- `knowledge/wiki/pending/`

Compilation rules live in `knowledge/schema/AGENTS.md`. PostgreSQL contains application
state and rebuildable search projections; it is not the source of truth for published Wiki
content.

## Repository layout

- `apps/api`: FastAPI API, Wiki repository, ingestion, search fusion, and LangGraph workflows.
- `apps/user-web`: end-user Wiki and Agent application.
- `apps/admin-web`: administration application.
- `packages/web-ui`: shared web UI components.
- `knowledge`: Git-backed Wiki and versioned schema.
- `storage`: immutable content-addressed source objects.
- `infra`: container initialization.

## Local development

Prerequisites:

- Python 3.12 or 3.13
- `uv`
- Node.js 22+
- `pnpm` 10+
- Git

Install dependencies:

```bash
uv sync --package llm-wiki-agent-api --group dev
pnpm install
```

Create local configuration:

```bash
cp .env.example .env
python - <<'PY'
import secrets
from cryptography.fernet import Fernet

print("WIKI_AGENT_SECRET_KEY=" + secrets.token_urlsafe(48))
print("WIKI_AGENT_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
PY
```

Copy those generated values into `.env`. For local API development without PostgreSQL, set:

```dotenv
WIKI_AGENT_DATABASE_URL=sqlite+pysqlite:///./wiki-agent.db
WIKI_AGENT_KNOWLEDGE_ROOT=./knowledge
WIKI_AGENT_STORAGE_ROOT=./storage
```

Run the API and web applications:

```bash
uv run --package llm-wiki-agent-api uvicorn wiki_agent.main:app --reload
pnpm dev:user
pnpm dev:admin
```

The API is available at `http://localhost:8000`, the user application at
`http://localhost:3000`, and the admin application at `http://localhost:3001`.

The default API installation is intentionally lightweight. Install the document parser
dependencies only for a local parser worker:

```bash
uv sync --package llm-wiki-agent-api --group dev --extra document-parsing
```

The initial administrator is created from
`WIKI_AGENT_BOOTSTRAP_ADMIN_EMAIL`/`WIKI_AGENT_BOOTSTRAP_ADMIN_PASSWORD`. Remove the
bootstrap password from the environment after first login.

## Docker Compose

Generate `.env` as described above, then run:

```bash
docker compose up --build
```

On macOS, a lightweight Docker CLI and Colima setup can be installed and started with:

```bash
brew install docker docker-compose docker-buildx colima
colima start --cpu 4 --memory 6 --disk 60 --runtime docker
```

If Homebrew-installed Docker plugins are not discovered, add
`/opt/homebrew/lib/docker/cli-plugins` to `cliPluginsExtraDirs` in
`~/.docker/config.json`.

The default database image packages PostgreSQL, `pg_search`, and `pgvector`. The current
Compose definition has been exercised with PostgreSQL 18, `pg_search` 0.25.9, and
`pgvector` 0.8.4, including the BM25 `@@@`/`paradedb.score()` query and vector distance
query used by the application. Pin `PARADEDB_IMAGE` to an approved release in production.

Compose runs separate lightweight API and general-purpose worker containers plus a
CPU-only parser worker. Parsing tasks use the `parser` Celery queue; compilation, crawling,
and other tasks use `default`. The parser worker is limited to one concurrent task in the
default Compose profile to avoid running multiple Docling/OCR pipelines within a small
Docker VM.

Document parsing uses the least expensive suitable parser first:

- Markdown and text use the built-in text parser.
- Simple HTML uses BeautifulSoup.
- DOCX files without tables use python-docx.
- Text PDFs without detected tables use PyMuPDF.
- Complex HTML, table-heavy documents, scanned PDFs, and text-sparse PDFs fall back to
  Docling.

The parser worker image contains the optional parsing stack and CPU-only PyTorch wheels;
the API and default worker image contains neither Docling nor PyTorch.

## Core workflow

1. An administrator configures encrypted chat, embedding, and optional reranker profiles,
   then assigns them to compilation, answer, embedding, and reranking purposes.
2. A user uploads a file, adds a webpage, or starts a bounded same-origin crawl.
3. The parser worker converts an immutable source revision into stable source spans,
   selecting a lightweight parser when possible and falling back to Docling for complex
   documents.
4. The default worker runs compilation, link validation, conflict detection, and
   verification through LangGraph. PostgreSQL deployments persist each graph checkpoint
   under the Agent run ID.
5. A separate verifier checks the proposed files. Deterministic rules evaluate citations,
   conflicts, source trust, change size, links, and concurrency.
6. The task creator reviews the draft, unless `low_risk_auto_publish` was requested and all
   checks pass.
7. Publication creates a Git commit and rebuilds the PostgreSQL Wiki, graph, citation, BM25,
   and vector projections.
8. Queries use Wiki BM25, graph traversal, and optional vectors, followed by RRF and an
   optional reranker. Raw source spans are queried only when the Wiki has insufficient
   evidence, and that fallback is labeled in the response. Fallback and zero-result
   queries are recorded for the administrator-only knowledge-gap report.

Interactive API documentation is available at `http://localhost:8000/docs`.

SQLite remains useful for local development and tests, but LangGraph checkpoints are not
persisted in that mode. Use PostgreSQL when compilation must resume reliably after a
worker restart.

## Knowledge publication contract

Every page:

1. starts with validated YAML frontmatter;
2. belongs to one approved page directory;
3. uses stable source revision/span citations;
4. has valid bidirectional links;
5. is published as a Git commit;
6. never silently overwrites a conflicting claim.

Drafts record their base commit. Publication fails on a stale base instead of overwriting
concurrent changes.

## Validation

```bash
uv run --package llm-wiki-agent-api ruff check apps/api
uv run --package llm-wiki-agent-api pytest
pnpm lint:web
pnpm build:web
```

## Security notes

- Replace all example secrets before exposing the service.
- Source URLs are restricted to public HTTP(S) addresses and validated at every redirect.
- Site crawls are bounded by same-origin, robots.txt, depth, page count, include/exclude
  path patterns, response size, and request timeout.
- Source content is untrusted data, never Agent instructions.
- Model API keys are encrypted at rest using `WIKI_AGENT_ENCRYPTION_KEY`.
- Cookie-authenticated mutations require a double-submit `X-CSRF-Token`.
- Only a draft creator can publish that draft.
- Raw HTML is not accepted as trusted Wiki output.

## Project status

The repository contains an executable first vertical slice: authentication and
administration APIs, encrypted model profiles and assignments, immutable file/web
ingestion, bounded site crawling, Git-backed Wiki publication, deterministic publication
risk rules, independent LLM verification, persistent BM25/graph/vector projections, RRF
and optional reranking, raw-RAG fallback, structured cited answers, Celery tasks, SSE
events, and separate user/admin web applications.

Before treating the project as production-ready, run workload-specific retrieval and
compilation evaluations, configure backups, pin container/model versions, and validate the
chosen pinned PostgreSQL extension combination on the deployment platform.
