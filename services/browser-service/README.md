# Wiki browser import service

Internal HTTP service (port 8080), Node **22+**, Playwright **1.58.2** and matching
`mcr.microsoft.com/playwright:v1.58.2-noble` image (official image supplies Node 24).
The Dockerfile explicitly verifies Node is at least 22.

## Authentication and API

Only `GET /health` is unauthenticated. All other requests require
`Authorization: Bearer <JWT>` signed using HS256 and `WIKI_AGENT_SECRET_KEY`.
Required claims: `aud: "wiki-browser-import"`, `iss: "wiki-agent"`, string `sub`
(user ID), string `wiki_id`, integer `exp`. Expiry must be in the future and no
more than 300 seconds away. Optional `iat`/`nbf` are checked. Issue a fresh token
for each interaction as necessary; a session lasts 15 minutes independently.
Live streams may use `scope: "browser-frames"` with a 32-character `session_id`,
required integer `iat`, and a maximum 900-second lifetime. These credentials
permit only `GET /sessions/<session_id>/frames`; they cannot create sessions,
capture content, send input, delete sessions, or read a different session.
The API uses these read-only credentials for the 15-minute viewer while keeping
ordinary control credentials short-lived. Session TTL still bounds stream expiry.
The development secret matches API Settings; it is rejected when `WIKI_AGENT_ENV`
or `NODE_ENV` is anything other than `development`. Compose interpolates only
`WIKI_AGENT_SECRET_KEY` and `WIKI_AGENT_ENV` from its automatically loaded `.env`;
it deliberately does **not** give this container the whole `.env` via `env_file`.
API/worker `env_file` settings are unchanged. Compose defaults to development for
existing local startup; set `WIKI_AGENT_ENV=production` with a real shared secret
in production.
Tokens are checked again when queued operations actually start.

| Method | Path | Input / result |
| --- | --- | --- |
| GET | `/health` | `{status:"ok"}` or 503 |
| POST | `/render` | `{url}` → 200 `{url,title,html}`; always closes context |
| POST | `/sessions` | `{url}` → 201 metadata |
| GET | `/sessions/:id` | 200 metadata |
| GET | `/sessions/:id/screenshot` | 200 PNG; viewport only; `no-store` |
| GET | `/sessions/:id/frames` | 200 live JPEG SSE; owner-scoped; `no-store` |
| POST | `/sessions/:id/actions` | action → 200 metadata |
| GET | `/sessions/:id/capture` | 200 `{url,title,html}` |
| DELETE | `/sessions/:id` | 204; closes context |

Metadata: `{id,url,title,width:1024,height:768,expires_at:<ISO UTC>}`.
Session creation returns at the initial public document commit (following the
service's HTTP redirect trampoline chain), not DOMContentLoaded/network idle.
The title can initially be empty; frames/title updates arrive while the document
and its resources continue loading. A first frame never advertises `about:blank`.
Titles are clipped to 512 characters; both input and canonical/output URLs are
limited to 8192 characters (oversized URLs are rejected, not truncated).
Actions: `click` with finite viewport `x,y`; `type` with 1–4096 character `text`
(inserts into the currently focused field); `key` with `key`; `scroll` with
integer `delta` (-3000 to 3000 vertical pixels); `reload`; `drag` with integer
`x,y,to_x,to_y`, all within the viewport (x 0–1023, y 0–767). Drag uses mouse
down, 1–32 bounded movement steps and mouse up, with guaranteed release on
movement failure. Endpoints never detect or automatically solve a CAPTCHA:
the caller supplies both drag endpoints from the user's pointer gesture.
No extraneous fields.
For live dragging use `pointer` with `phase: "down" | "move" | "up"` and integer
`x,y` in the same viewport. Each request dispatches real mouse events, retaining
the button state between requests.
`move` without a held button is normal hover (including CSS hover menus); the
same phase preserves a held button during dragging. `up` always moves to its
final `x,y` before releasing, so a coalesced/throttled final move is not lost.
When the last viewer disconnects, or an owned action fails/is aborted, held mouse
buttons are released across the context's tabs. An input generation invalidates
old queued actions (including text) before dispatch, so reconnect cannot replay
stale input. In-flight mouse-down races are also released; fresh input waits for
the release to finish. This reset preserves the session, pages and login cookies.
`back` initiates safe browser history
navigation; a missing public HTTP(S) history entry returns 400 `NO_HISTORY`.
Actions acknowledge input immediately with cached metadata, without waiting for
network idle, a fixed sleep, page navigation or a screenshot. After click/key/
reload/back, the acknowledgement may contain the preceding URL/title until the
navigation commits; streamed frames are authoritative for the displayed page.
Explicit capture waits for document readiness and one paint, not network idle.
Only automatic `/render` retains the bounded render settling wait.
Allowed keys: Enter, Tab, Shift+Tab, Escape, Backspace, Delete, ArrowLeft,
ArrowRight, ArrowUp, ArrowDown, Home, End, PageUp, PageDown, Space, Control+A,
Meta+A. Click a login/CAPTCHA field, insert text, and press Enter or click its
button. CAPTCHA solving is **only manual**, never automatic.

Errors are `{detail:{code,message}}`; 401 invalid token, 404 missing/expired/
foreign session (same user **and** wiki required), 400 invalid URL/action,
413 size/resource budget, 429 session/queue limit, 502 loading failure,
504 timeout. No input, stack, page, cookies or credentials are logged/persisted.
Compatible codes: `SESSION_EXPIRED`, `SESSION_NOT_FOUND`, `SESSION_NOT_OWNED`
(all 404), `BROWSER_CAPACITY` (413/429/503), `PUBLIC_URL_REQUIRED` (400),
`UNSAFE_URL` (400), `BROWSER_RENDER_FAILED` (502/503/504). Unknown failures use
the constant `BROWSER_RENDER_FAILED` with a fixed safe message, never a raw
exception. Other validation codes are uppercase (e.g. `INVALID_ACTION`).
Bounded, memory-only expiry tombstones (at most 128, retained for 15 minutes)
allow `SESSION_EXPIRED` after context cleanup; no cookies or action data are kept.
Capture removes scripts, form wrappers, fields, embedded frames and event-handler
attributes (including template contents); article/content nodes are retained.
Screenshots and live frames intentionally show the active page and must be treated
as sensitive. Public HTTP(S) popup/tab links are supported in the same owned
context (at most four tabs). The newly opened page becomes active; closing it
restores its opener, or the most recent surviving page if the opener is gone.
Downloads/dialogs and webpage WebSockets remain disabled. Website-issued
Authorization headers generated by the webpage are supported, but any JWT
declaring this service's issuer/audience is stripped; API request headers are
never copied into browser contexts.

## Realtime frame protocol

Use the app's authenticated proxy rather than an extension: create a session,
open its frame stream, dispatch direct pointer/typing/key/scroll actions, and
capture once the user confirms import. Authentication is the same Bearer JWT
used by other endpoints; tokens are **not** accepted in the query string.

`GET /sessions/:id/frames` returns `text/event-stream` with
`Cache-Control: no-store, no-transform` and `X-Accel-Buffering: no`. Frames are:

```text
event: frame
data: {"image":"<base64 JPEG without a data-URL prefix>","url":"https://example.com/article","title":"Article","width":1024,"height":768,"can_go_back":false}

```

CDP `Page.startScreencast` uses JPEG quality 65, viewport 1024×768, with output
capped at 8 fps. The raw base64 JPEG starts with `/9j/` and decodes to the full
1024×768 viewport. A one-shot JPEG fallback ensures a prompt initial frame on
static pages; there are **no per-action screenshots**. Every CDP frame is
acknowledged promptly; intermediate frames are dropped/coalesced. Each session
keeps at most one latest frame and one pending image, at most 1 MiB base64 each.
At most two stream subscribers are allowed per session. A slow socket holds
only its latest pending frame while waiting for drain, never an unbounded queue.
No frame, credential or action text is logged or written to storage.

A `: heartbeat` comment is emitted every **10 seconds** while the stream is writable,
including unchanged/static pages, keeping a 30-second upstream read timeout alive.
After headers, terminal failures use `event: error` with
`data: {"code":"...","message":"..."}`, then close the stream. Before
headers, errors remain normal JSON with the corresponding HTTP status.
Owner/existence validation and stream-capacity reservation precede HTTP 200.
`can_go_back` is an optional boolean, initially false until known, refreshed via
CDP navigation history on URL changes; it does not add per-input history queries.
Ownership/expiry are rechecked for every emitted frame and heartbeat. Streams
close when their JWT expires, the 15-minute session TTL is reached, or the
session is deleted/shut down. Reconnect with a fresh token; the session's
ephemeral login state remains until deletion/expiry. No event IDs or frame
history/replay are provided. Disconnect stops and detaches the screencast once
the final subscriber leaves; reconnect starts a healthy new screencast.
Popups switch the active CDP target without disabling routing/proxy isolation.

## Network and resource boundaries

Every HTTP(S) browser request is intercepted and fetched by Node, with every DNS
answer checked as public and the chosen address pinned to the actual socket.
The original hostname is retained for TLS verification and Host. Redirect
destinations are validated before following Location and pinned anew on each
request. Because Chromium bypasses Playwright routing on native HTTP redirect
follow-ups, document redirects use a minimal new-navigation trampoline (preserving
POST/body for 307/308); subresource redirect chains are fetched entirely in Node.
Cookies on cross-origin subresource redirects are omitted conservatively.
Private, loopback, link-local, multicast, special-purpose addresses,
IPv4-mapped IPv6 and credential-bearing URLs are rejected. A rejecting local
Chromium proxy blocks unrouted requests. Service workers, QUIC and nonproxied
WebRTC UDP are disabled. Service Authorization/owner headers are never forwarded.
Chromium receives a minimal environment, not the service's secret/API environment.
Multiple Set-Cookie headers are validated independently and applied to the
ephemeral browser cookie store, including redirects, deletion and login POSTs.

Limits: 8 total contexts (including render), 2 per user across wikis, 15 minute
absolute TTL, serialized session operations, 16 queued operations per context;
256 requests / 64 MiB per context, 8 simultaneous fetches, 4 MiB per response
(both compressed and decoded), 1 MiB page POST data, 10 redirects, 10 second
fetch/DNS deadline, 25 second navigation and 35 second overall operation deadline.
HTTP input is capped at 16 KiB. Capture and screenshot are capped at 4 MiB.
Compose caps CPU/memory/PIDs at **1 CPU / 1 GiB / 256 PIDs**, uses `pwuser`,
no-new-privileges and drops all
capabilities. No host port is published.

Unsupported or budget-exceeding resources fail closed. WebSocket-dependent sites,
very large pages and anti-headless challenges may not work; do not
add automatic CAPTCHA bypass to compensate. This is a content import browser,
not a general-purpose remote desktop.

Ordinary resource bursts wait for one of the eight fetch slots rather than
failing immediately. Closing a context cancels queued fetches. A failed image,
font, stylesheet or embedded resource is blocked and reported without terminating
the main page or live stream. The frame's optional `resource_errors` count and
privacy-safe `browser.resource_failed` logs surface these failures. Main-document
and unsafe navigation failures remain fatal; network restrictions and byte/request
budgets are unchanged.

## Local validation

Run `npm ci --ignore-scripts` then `npm test` in this directory. Tests inject
DNS/transports/browser adapters; the real-browser test uses only synthetic public
URLs with mocked DNS and transport and never contacts those hosts. If Chromium is
not installed, that test reports a skip. Set `BROWSER_TEST_REQUIRED=1` to require it.
To install the pinned browser entirely within this package and require all tests:

```sh
mkdir -p .browser-download
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" TMPDIR="$PWD/.browser-download" npx playwright install chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" BROWSER_TEST_REQUIRED=1 npm test
```

The hardened image was validated with Node 24.13.0 and all 33 tests passing,
including real Chromium rendering, manual login, cookie/redirect handling and
network isolation. A real-Chromium HTTP integration test exercises session
creation, metadata, screenshots, manual typing/keys/drag, capture, relative-link
navigation, automatic render and deletion. It also deliberately creates an
unrouted context and confirms the rejecting proxy returns HTTP 403 and rejects
HTTPS tunneling without calling the public fetcher. Redirect tests verify
browser-visible final URLs and directory-relative links/CSS resources.
Image startup/health/authentication and graceful shutdown were
also smoke-tested with no network access or published ports.
Run `npm start` only with Chromium installed and suitable shared secret/environment;
use the same `PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"` prefix for a package-local installation.

### Measured latency

The controlled real-Chromium fixture runs with mocked public DNS/transports,
`--network none` and the unchanged 1 CPU / 1 GiB / 256-PID bounds:

| Metric | Before | After |
| --- | ---: | ---: |
| Session open | 960 ms | 154 ms |
| Input action median (24 samples) | 306 ms | 3 ms |
| Input action p95 (same fixture) | 319 ms | 16 ms |

The old path always slept 300 ms after each action and could additionally wait
up to 1500 ms for network idle; a separate screenshot was then needed. These
waits are removed from interactive open/input. Legacy explicit screenshots
remain available, but should not be used as the live rendering loop.

With an animated page, real HTTP actions and active SSE (41 action samples),
measured action p95 was **55 ms**, pointer p95 **41 ms**, first frame **238 ms**,
and reconnect **33 ms**. A fixture delayed reload response by 700 ms and still
acknowledged reload within 250 ms. The tests enforce action/pointer p95 under
250 ms where feasible on this fixture; these are measurements, not a latency
guarantee for arbitrary websites or concurrent users. No CPU increase was needed.
`test/measure-latency.js` reproduces the standalone fixture; `realtime.test.js`
measures the full HTTP/stream path and verifies initial/update frames, direct
input, pointer drag, popup/redirect/back, reconnect, JWT/TTL expiry and bounds.
The heavy-document fixture delays a blocking script by 1500 ms: session open
returned at commit in 102 ms and a valid public-URL JPEG frame arrived before
that script completed. Disconnecting the stream preserved the owned session;
explicit deletion subsequently closed it.
Live tests also disconnect while dragging, reconnect and verify normal hover;
invalid-action and in-flight-down/stale-generation tests verify reset without
typing old queued private text or destroying the context.
