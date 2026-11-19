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
- `packages/web-ui`: shared web UI components.
- `knowledge`: Git-backed Wiki and versioned schema.
- `storage`: immutable content-addressed source objects.
- `infra`: container initialization.

The deployment uses one account and one shared Wiki repository. Published pages are stored
in `knowledge/wiki/` and versioned together with the compilation rules in `knowledge/`.
After login, the web app initializes the personal knowledge library automatically using
the idempotent `POST /api/v1/wikis/initialize` endpoint. Users do not enter a library name
or description. The sidebar always displays **个人知识库**; existing names, library IDs,
and source material are preserved internally, and initialization never deletes old libraries.
On first access after upgrading, pages from that account's legacy
`knowledge/wikis/<wiki-id>/` repository are migrated into the shared Wiki repository; the
legacy repository is retained as a backup.
The **知识 Wiki** page presents published knowledge as a searchable relationship graph:
filter page and relationship types, hide isolated pages, adjust node spacing, and open a
page in the adjacent reader. The compact bottom-right minimap uses colored dots and a
blue viewport outline when more than five nodes are visible, and supports panning and zooming.
All six workspace tabs fill the area beside the navigation and below the header without
outer gutters; readers and conversations scroll within their available workspace.
Wiki, questions, and knowledge rules use the topbar's page label without duplicate
content banners; the question's editable conversation title has no separator below it.
The application supports PC browsers only: navigation remains in the left sidebar,
and there are no phone/tablet layouts or touch-specific interactions. Runtime logs use
server-side numbered pagination with 20 rows per page by default and 10/20/50/100 choices.
**导入与原件** combines file/folder/URL import, processing
status, and original-file preview. Click an original to read
PDF or text/HTML, or extracted DOCX text, and download the original file. Source
citations on a Wiki page link back to the corresponding original.
Original-file and cited-span previews preserve the parsed Markdown heading levels;
heading metadata is not inserted again into the displayed content.

The web UI uses a shared blue-accent design with light and dark themes. Use the theme
button in the top bar (also available on the login and password-recovery screens) to
switch themes. The first visit follows the system color scheme; an explicit choice is
saved locally and applied before rendering to avoid a theme flash. On small screens,
the main navigation moves to the bottom and the import and question panels stack.
Shared theme colors and component styles live in `packages/web-ui/src/styles.css`.

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
```

The API is available at `http://localhost:8000` and the user application at
`http://localhost:3000`.

The first start creates one application account: username `admin`, initial password
`WikiAdmin123!`, and no email address. After signing in, changing the initial password
and binding an email are recommended but optional. You can bind an email address from
Personal Center; binding requires the configured SMTP service and cannot be undone.
Until an email is bound, password recovery by email is unavailable. Override
`WIKI_AGENT_INITIAL_ADMIN_USERNAME` and `WIKI_AGENT_INITIAL_ADMIN_PASSWORD` in `.env`
before the first start if you want different initial credentials. Existing databases
with users are not reseeded or overwritten.
Uploads require both the `default` and `parser` Celery workers (and Redis) to continue
past storage, plus an answer-capable chat model assigned to **compilation** for the
current user. Running only the API and web applications does not process queued imports;
`docker compose up --build` includes both workers. The Wiki source list shows each
document's actual compilation state and any processing failures rather than treating
an uploaded revision as a published Wiki page. Website imports save articles but do not
queue compilation automatically; use the source card's **编译** action to start compilation.
Docker application source and frontend bundles are baked into images, not mounted from
the workspace. After changing API/compiler or frontend code, rebuild and recreate the
affected services; a browser refresh or container restart alone does not update the code:

```bash
docker compose build api user-web
docker compose up -d --no-deps api worker user-web
```

Wait for active processing runs to finish before replacing workers. Parser changes also
require rebuilding and recreating `parser-worker`.
PostgreSQL checkpoint setup performs concurrent index migrations; compilation closes its
source-read transaction before setup so the worker does not block on its own transaction.
First-use checkpoint migrations are serialized between workers.
Parsing, compilation, verification, indexing, and embedding backfill also finish read
transactions before slow model calls, so concurrent checkpoint setup is not held up by
idle database sessions waiting for model responses.
The import dialog accepts multiple documents, a folder (including nested folders), or
dragged files/folders. One multi-file gesture is submitted to `POST /api/v1/sources/batch`;
valid PDF, DOCX, Markdown, TXT, and HTML files are stored individually, while unsupported
or empty files are reported by name rather than silently dropped.
All accepted revisions share one compilation run. The parser prepares every member,
then compilation incrementally merges each member into the same staged proposal, using
earlier batch summaries/pages as context. Only after all members succeed does independent
verification produce one draft; a failure retains the originals without creating partial
review drafts. Batch imports and multi-source compilation always require one manual
approval, regardless of the single-file automatic-publication flag.
Approving publishes all proposed files in one Git commit; rejecting deletes the batch's
original sources and processing records together, unless existing reference safeguards block
the entire decision. Published pending pages from the same batch are shown as one conflict
group with links to each page. Independent historical drafts are not guessed into batches.
Each source card can retry a failed or uncompiled revision using the currently assigned
compilation model; retries create a new run rather than reusing the failed run. Deletion
removes an idle source's versions, processing history, and unshared stored objects even
when Wiki pages or drafts reference it. Source-only pages and drafts are deleted;
shared pages retain their other sources, while deleted-source citations, exclusively
supported text, backlinks, search documents, and graph relationships are cleaned up.
Retrying one failed batch member retries the remaining batch as a group. Deleting an idle
member removes its evidence but retains the other members' shared run/draft association.
Concept pages keep their text and type while any source remains, even if deletion
leaves only one source; new concepts still require two independent sources.
Compilation supplies an explicit list of available source summary paths, including the
current revision's summary created in the same proposal. Both model output modes instruct
the model to compare current evidence with existing summaries, declare the supporting
paths in each concept's `sources`, and cite evidence from each source using exact span IDs.
Unrelated documents and multiple spans from one source do not supply a second source.
If a concept lacks joint support, its facts stay in the source summary or an appropriate
entity page instead. Invalid model output still blocks publication; retrying an old failed
import applies the updated instructions but does not guarantee model compliance.
Model instructions also spell out Wiki-relative paths for files, `sources`, and `related`:
one approved directory and a `.md` filename, with no `wiki/` prefix or nested directories.
New filename stems use lowercase ASCII letters, digits, and hyphens; titles can be Chinese.
Deletion remains refused while processing to prevent workers from recreating deleted data.
The deletion dialog shows only the source title and an irreversible-deletion warning.
Confirming immediately closes the dialog and deletes the source in place without
another dialog or page navigation.
The source list supports individual selection and **全选当前列表**, scoped to the current
search results. Selected sources can be compiled or deleted in a batch; compilation skips
ineligible sources, deletion requires confirmation, and partial failures identify the affected
sources instead of reporting the whole batch as successful. A compact toolbar beside the
select-all checkbox shows the selection count and compilation/deletion actions. Click a
filename in the source list or preview header to edit it inline; Enter or leaving the
field saves, and Escape cancels. Renaming changes only the source's displayed name;
immutable original bytes, revisions, and citation IDs are unchanged.
If deletion fails, the current page shows the specific failure and refreshes its data; a
reference conflict is not reported as a changed Wiki revision.

### Website imports

Website import follows one flow: enter a URL, choose **开始导入**, interact with the live
page, and choose **确认导入当前页面** when the desired article is visible. The dialog no
longer separates automatic import and browser import. Click an input and type normally;
Chinese composition, paste, keyboard navigation, wheel scrolling and manual slider
dragging are supported directly in the page. The display updates continuously, including
after a QR-code login, without a separate refresh-image button.

The viewer uses a bounded JPEG screencast rather than full PNG screenshots after each
action. Input operations are ordered, redundant pointer movements are coalesced, and
ordinary interactions do not disable the whole viewer while waiting for a redraw.
Shift+Esc returns keyboard focus to the dialog controls.

Confirmation extracts the current page's article title and main text, removes navigation
and comments, and supports WeChat public-account markup. Verification pages, login screens
and empty pages are not saved as articles. Verification is never solved automatically,
and some sites may still refuse remote/headless browsers. Only the current article is
imported; compilation remains a separate explicit action.

Sessions expire after 15 minutes and are cleared on close; cookies and typed credentials
are not persisted. Login token query parameters are removed from the saved source URL.
The service permits public HTTP(S) destinations only, including redirects and embedded
resources, and applies request/session limits. It is not exposed directly to the host:
the authenticated API proxies browser controls and the live frame stream.

Docker Compose includes the `browser` service and configures API/worker access automatically.
The website dialog requires that service. Static article and bounded same-site crawling
APIs remain available for integrations. To connect
a locally reachable service, set `WIKI_AGENT_BROWSER_SERVICE_URL` to its internal base URL
and use the same `WIKI_AGENT_SECRET_KEY` as the API; never expose the service publicly.
OpenAI-compatible chat endpoints use explicit function calling for structured results
except for DeepSeek's official endpoint, whose default thinking mode does not accept
forced `tool_choice`. Its structured results use `response_format=json_object` with a
JSON-schema prompt and Pydantic validation instead. The question agent's actual
knowledge-search tools still use tool calling.
Compilation requires full Markdown Wiki pages with YAML frontmatter and declared span
citations. If the model returns plain headings instead, validation rejects the draft
and stores a concise, non-source-revealing error rather than the raw model response.
Validation errors identify unsupported concept pages, invalid pending-page status,
unsafe Markdown, duplicate paths, and invalid response fields without storing model
text. These failures remain blocked from publication; retrying does not bypass validation.
The linking stage completes missing reverse relationships for explicitly linked pages
in the proposal and its base revision. It changes relationship metadata only, preserving
page bodies, sources and citations. Missing/deleted targets remain validation failures;
the system never invents a page to satisfy a link.
For DeepSeek JSON mode, the model returns page paths, titles, Markdown bodies, and
source citations. The server derives page IDs, types, and timestamps from the path
and existing pages, builds YAML frontmatter, and validates every page before
publication. Other compatible providers continue to return complete Markdown pages.
If a configured model times out, the source card reports the timeout separately from
parser, queue, and Wiki-format failures. The original source remains available to retry.
Compilation's page-generation and verification model calls both allow 180 seconds
per attempt by default (`WIKI_AGENT_COMPILATION_MODEL_TIMEOUT_SECONDS`); question
answering and other chat calls retain their 60-second per-attempt timeout. The model's reasoning
mode is unchanged. Existing failed runs must be explicitly retried after changing
this setting.

The default API installation is intentionally lightweight. Install the document parser
dependencies only for a local parser worker:

```bash
uv sync --package llm-wiki-agent-api --group dev --extra document-parsing
```


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

1. In **模型配置**, configure encrypted chat, embedding, and optional reranker profiles,
   then assign them to compilation, answer, embedding, and reranking purposes.
2. A user uploads a file or adds a webpage. The original bytes are committed to immutable,
   content-addressed storage before any processing begins, and an automatic compilation
   run is queued immediately.
3. The parser worker converts the immutable source revision into stable source spans,
   selecting a lightweight parser when possible and falling back to Docling for complex
   documents.
4. The default worker applies `knowledge/schema/AGENTS.md`, creates the mandatory source
   summary page, incrementally updates matching entity and concept pages, and routes
   contradictions to `pending/`. Existing Wiki page content is supplied as compilation
   context so updates merge with prior knowledge rather than replacing it blindly.
5. A separate verifier checks the proposed files. Deterministic rules evaluate citations,
   source-page references, conflicts, source trust, change size, links, and concurrency.
6. Low-risk single-source changes are published automatically when automatic publication is enabled.
   conflicts and high-risk changes remain as drafts for review by their creator. In the
   **审核冲突** page, the Wiki owner can
   preview every changed file and publish with an optional message, or give a reason
   and reject. Rejection requires confirmation and permanently deletes this draft,
   its originating imported source(s) (including revisions, parsed text and processing
   records), and unshared original files. Published Wiki pages and other sources are
   never deleted; if a published page or another draft references the originating
   source, rejection is blocked without deleting anything. Sources are identified
   from the draft's compilation run, not from citations to unrelated documents.
   Only the draft creator can invoke these actions. Published `pending/`
   pages appear separately below the draft list; unpublished conflicts appear only
   within their drafts until approved. Publishing updates the Git Wiki and rebuilds
   the search index; if indexing fails after publication the UI reports that partial
   failure explicitly.
   The publish-message field is visible as soon as the review dialog opens. Leaving it
   blank uses a Git commit message based on the draft title; rejection still requires a reason.
7. Publication creates a Git commit and rebuilds the PostgreSQL Wiki, graph, citation, BM25,
   and vector projections.
   The user Wiki page displays the published pages as a titled, force-directed
   knowledge graph (source, entity, concept, and pending nodes). The layout stays stable
   across status refreshes for the same Wiki revision. Hovering or focusing a node
   highlights its direct neighbors and links; search locates nodes by title, and the graph
   can zoom or fit to the viewport. Selecting a node opens its page rendered as rich text
   by default, with a Markdown source toggle and links to connected pages.
   `GET /api/v1/wiki/graph` reads the account Wiki's published pages and returns
   titles and deduplicated source/related edges; it does not expose unreviewed drafts.
8. `/wiki/search` uses the assigned answer model to produce lexical and semantic retrieval
   queries, then runs Wiki BM25, graph traversal, and optional vectors followed by RRF. It
   checks Wiki coverage before adding Raw results and conditionally reranks. `/questions`
   uses a bounded, read-only tool-calling agent instead; the model chooses when to search,
   read a Wiki page, expand graph links, or inspect original source spans. Both record
   retrieval activity for personal history.
9. Embedding or reranker transport failures degrade explicitly to the remaining retrieval
   channels or RRF order. Changing the assigned embedding model requires rebuilding the
   Wiki index; existing raw spans can be updated with the source-embedding backfill job.
10. **知识规范** edits the rules used during compilation. **运行日志** shows the signed-in
    user's operation history and API, background-task, and question errors.
    Each entry has a short action-specific explanation. Original documents and published
    Wiki pages link to their readers by real titles; file types/sizes are human-readable,
    and internal paths or HTTP diagnostics stay in collapsed technical details. Unknown
    failures do not claim a save/publication outcome. Repeated reporting within the same
    HTTP request is suppressed, but independent attempts remain separate.

Interactive API documentation is available at `http://localhost:8000/docs`.

### Question conversations and agent boundaries

- `POST /api/v1/questions` accepts `query` and optional `conversation_id`/`request_id`.
  A stable `request_id` makes retries idempotent. The response contains a conversation ID,
  generated conversation title, run ID, message ID, citations, and optional clarification question.
  The POST remains synchronous; the browser polls the persisted run independently while it
  waits, so status, titles, and cancellation do not depend on the long POST finishing. It also honors
  `embedding_profile_id`, `reranker_profile_id`, `allow_raw_fallback`, `entry_node_ids`,
  and `limit`; the deterministic `/wiki/search` options `minimum_core_hits` and
  `require_cross_channel_top` do not apply to the autonomous agent.
  The web app renders answers as Markdown. Inline citations and reference cards
  link only to cited evidence: a Wiki page or the exact original revision and
  span. Older answers without inline citation markers still have reference cards
  when the saved evidence includes a target. When no evidence is found, the UI
  explains the limitation in Chinese and suggests clarifying or importing sources.
- `POST /api/v1/questions/conversations` creates an empty conversation in the selected
  Wiki. On its first question, the answer model generates a concise title in the user's
  language before retrieval; it is saved immediately and sent to the browser in run status
  and answer responses. Later turns and recovery keep that title. The user web app keeps the active
  conversation across reloads, lists the Wiki's conversations, and restores stored turns
  when a conversation is selected. Use the `+` beside **历史对话** to start a conversation;
  if no conversation is active, submitting a question automatically creates one using the
  same idempotent question request. The current conversation's title appears above the
  messages, beside the history panel. Click the title to edit it inline; Enter or leaving
  the field saves, and Escape cancels. History rows support deletion after confirmation.
  `PATCH /api/v1/questions/conversations/{id}` accepts a
  trimmed, single-line `title` of 1–120 characters. Manually chosen names survive reloads,
  later questions, recovery, and an in-flight automatic title generation.
- `GET /api/v1/questions/conversations` lists conversations in the selected Wiki.
  `GET` and `DELETE /api/v1/questions/conversations/{id}` read and remove an owned
  conversation; `GET /api/v1/questions/runs/{id}` reports its stage, title, and whether
  recovery is allowed. `GET /api/v1/questions/requests/{request_id}` finds an owned run
  even if the initial POST response was lost; it returns `null` before the run is created.
  The browser saves the question and request ID before submission, so reconnects and
  reloads can recover that same run rather than starting another question.
- `POST /api/v1/questions/runs/{id}/cancel` permanently stops a running or failed question.
  Cancellation invalidates its execution lease immediately; an already-issued model or tool
  request may finish, but its result cannot publish an answer or execute the next step.
  Cancelled questions cannot be resumed. Cancelling an already-completed question preserves
  its answer. Starting a new question remains possible after cancellation.
- Failed runs and running runs without checkpoint activity for five minutes can be recovered
  via `POST /api/v1/questions/runs/{id}/resume`. SQL checkpoints persist the generated title,
  completed memory-compression chunks, history, resolved question, planning messages, pending
  tool batch and next-tool position, tool-result cache, evidence, call budgets, reranking
  result, and generated answer. Recovery continues at the next unfinished operation rather
  than repeating completed retrieval. A model/tool request whose result was not checkpointed
  may need to be issued again; this is not token-level continuation of a provider's response.
  Checkpoints work with SQLite and PostgreSQL and survive API restarts. A fresh running
  question cannot be recovered concurrently, and an older execution lease cannot overwrite
  cancellation or a newer recovery attempt.
- The agent can call only `SearchWiki`, `ReadWikiPage`, `ExpandGraph`, `SearchRaw`, and
  `ReadSourceSpan`. Every tool is scoped by the authenticated user's selected Wiki;
  Raw tools honor the Raw RAG feature flag. No arbitrary file, shell, network, upload,
  editing, or publication tool is available to the model.
- A question is limited to eight tool calls, six planning steps, eight search hits per
  call, 8,000 characters per read, and 40,000 evidence characters. Resolution uses up to
  six recent messages plus a bounded topic/entity memory. Previous turns that do not fit
  the `WIKI_AGENT_QA_HISTORY_CONTEXT_BYTE_BUDGET` (6,144 bytes by default, including
  the current question) are compacted in chunks into topic memory; the original messages
  stay in the database. This also applies if the newest historical answer alone is too
  long. A cursor avoids recompressing already summarized messages. The current question
  is passed verbatim to reference resolution; tool planning uses an independently
  bounded, possibly shorter suffix of history and can use only the resolved standalone
  question when passing both question forms would exceed that budget.
  Compression errors fail explicitly rather than truncating a message without notice.
  This is a conservative byte budget, not a provider-specific token-window guarantee;
  tool results and final-answer evidence have separate limits. Prior answers inform
  reference resolution but are never treated as factual evidence. Ambiguous references
  require clarification; successful answers must cite evidence from this run.
- Upload and publication still write to the local source store and Git-backed Wiki via
  the existing API/worker paths. The tool registry limits what the model can invoke,
  but the current API process itself still has writable mounts. Enforcing OS-level
  read-only isolation for question execution requires a separate runtime with read-only
  mounts or an isolated knowledge-tool service.

SQLite remains useful for local development and tests, and question-agent SQL checkpoints
are persisted there. Compilation's LangGraph checkpoints are not persisted in that mode.
Use PostgreSQL when compilation must resume reliably after a
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
- Every user manages an isolated set of model profiles and purpose assignments in the
  user application. Model API keys are encrypted at rest using
  `WIKI_AGENT_ENCRYPTION_KEY`. The settings UI groups chat, embedding, and reranker
  profiles, provides common provider presets, supports editing existing profiles without
  re-entering unchanged credentials, and assigns defaults per workload.
- Account settings support avatar upload, display-name updates, password changes, and
  account deletion. Password changes revoke all sessions; account deletion anonymizes
  the account and removes its encrypted model credentials while preserving knowledge
  provenance and audit history.
- Cookie-authenticated mutations require a double-submit `X-CSRF-Token`.
- Only a draft creator can publish that draft.
- Raw HTML is not accepted as trusted Wiki output.

## Project status

The repository contains an executable personal knowledge workflow: authentication and
personal APIs, encrypted model profiles and assignments, immutable file/web ingestion,
bounded site crawling, Git-backed Wiki publication, deterministic publication
risk rules, independent LLM verification, persistent BM25/graph/vector projections, RRF
and optional reranking, raw-RAG fallback, structured cited answers, Celery tasks, SSE
events, and a single personal web application.

Before treating the project as production-ready, run workload-specific retrieval and
compilation evaluations, configure backups, pin container/model versions, and validate the
chosen pinned PostgreSQL extension combination on the deployment platform.
