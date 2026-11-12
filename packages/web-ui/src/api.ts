declare const process: { env: Record<string, string | undefined> };

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1"
).replace(/\/+$/, "");

export type ModelKind = "chat" | "embedding" | "reranker";
export interface User {
  id: string;
  username: string | null;
  email: string | null;
  display_name: string;
  avatar_url: string | null;
  updated_at: string;
  is_active: boolean;
  must_change_password: boolean;
}

export interface PersonalWiki {
  id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
}

export interface CaptchaChallenge {
  challenge_id: string;
  background_image: string;
  puzzle_image: string;
  image_width: number;
  image_height: number;
  puzzle_width: number;
  puzzle_height: number;
  puzzle_top: number;
  expires_in: number;
}

export interface CaptchaVerification {
  captcha_token: string;
  expires_in: number;
}

export interface CaptchaVerifyPayload {
  challenge_id: string;
  x: number;
  y: number;
  duration: number;
  trail: [number, number][];
}

export interface Source {
  id: string;
  name: string;
  kind: "pdf" | "docx" | "markdown" | "text" | "html" | "website";
  current_revision_id: string | null;
  trust_level: number;
  created_at: string;
}

export interface SourceRevision {
  id: string;
  source_id: string;
  sha256: string;
  media_type: string;
  size_bytes: number;
  status: string;
  created_at: string;
}

export interface SourceIngest extends SourceRevision {
  compilation_run_id: string;
  compilation_status: string;
  compilation_error: string | null;
}

export interface SourceBatchIngest {
  items: {
    file_name: string;
    revision: SourceIngest | null;
    error: string | null;
  }[];
  compilation_run_id: string | null;
  compilation_status: string | null;
  compilation_error: string | null;
}

export interface SourceSpan {
  id: string;
  source_revision_id: string;
  ordinal: number;
  heading: string | null;
  content: string;
  locator: Record<string, unknown>;
}

export interface BrowserSession {
  id: string;
  url: string;
  title: string;
  width: number;
  height: number;
  expires_at: string;
}

export type BrowserAction =
  | { action: "click"; x: number; y: number }
  | { action: "pointer"; phase: "down" | "move" | "up"; x: number; y: number }
  | { action: "drag"; x: number; y: number; to_x: number; to_y: number }
  | { action: "type"; text: string }
  | { action: "key"; key: "Enter" | "Tab" | "Backspace" | "Escape" | "Control+A" |
      "Meta+A" | "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown" }
  | { action: "scroll"; delta: number }
  | { action: "reload" | "back" };

export interface WikiPageSummary {
  revision: string;
  pages: string[];
}

export interface WikiCitation {
  source_revision_id: string;
  span_id: string;
  label: string | null;
}

export interface WikiPage {
  metadata: {
    id: string;
    type: "source" | "entity" | "concept" | "pending";
    title: string;
    created: string;
    updated: string;
    sources: string[];
    related: string[];
    citations: Record<string, WikiCitation>;
    tags: string[];
    status: string;
  };
  body: string;
}

export interface WikiGraphNode {
  path: string;
  title: string;
  kind: WikiPage["metadata"]["type"];
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  kind: "source" | "related";
}

export interface WikiGraph {
  revision: string;
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export interface WikiSearchHit {
  document_id: string;
  score: number;
  channels: string[];
  content: string;
  metadata: Record<string, unknown>;
}

export interface SearchDegradation {
  channel: string;
  reason: string;
}

export interface QueryRewrite {
  lexical_query: string;
  semantic_query: string;
}

export interface WikiRetrievalQuality {
  sufficient: boolean;
  hit_count: number;
  top_channels: string[];
  reason: string | null;
}

export interface WikiSearchResponse {
  query: string;
  layer: string;
  fallback_used: boolean;
  channels: string[];
  degradations: SearchDegradation[];
  query_rewrite: QueryRewrite;
  wiki_quality: WikiRetrievalQuality;
  reranker_used: boolean;
  hits: WikiSearchHit[];
}

export interface WikiQuestionResponse {
  answer: string;
  citations: string[];
  uncertainty: string | null;
  insufficient_evidence: boolean;
  layer: string;
  fallback_used: boolean;
  channels: string[];
  degradations: SearchDegradation[];
  query_rewrite: QueryRewrite;
  wiki_quality: WikiRetrievalQuality;
  reranker_used: boolean;
  evidence: Array<{
    document_id: string;
    channels: string[];
    metadata: Record<string, unknown>;
  }>;
}

export interface WikiQuestionSubmission extends WikiQuestionResponse {
  conversation_id: string;
  conversation_title: string;
  message_id: string;
  run_id: string;
  clarification_question: string | null;
}

export interface WikiQuestionConversationSummary {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface WikiQuestionMessage {
  id: string;
  role: string;
  content: string;
  citations: string[];
  created_at: string;
  answer?: WikiQuestionResponse | null;
}

export interface WikiQuestionConversation {
  id: string;
  title: string;
  messages: WikiQuestionMessage[];
}

export interface WikiQuestionRun {
  id: string;
  status: string;
  stage: string | null;
  error: string | null;
  conversation_id: string;
  conversation_title: string;
  question: string;
  request_id: string;
  resumable: boolean;
  answer?: WikiQuestionResponse | null;
}

export interface WikiIndexStatus {
  assigned_embedding_profile_id: string | null;
  indexed_embedding_profile_id: string | null;
  indexed_at: string | null;
  wiki_document_count: number;
  wiki_vector_count: number;
  raw_span_count: number;
  raw_vector_count: number;
  wiki_rebuild_required: boolean;
  raw_backfill_required: boolean;
}

export interface Draft {
  id: string;
  title: string;
  base_commit: string;
  files: Record<string, string | null>;
  status: "open" | "published" | "rejected";
  published_commit: string | null;
  created_by_id: string;
  created_at: string;
}

export interface AgentRun {
  id: string;
  kind: string;
  status: string;
  input_json: Record<string, unknown>;
  result_json: Record<string, unknown> | null;
  error: string | null;
  created_at: string;
}

export interface FeatureFlag {
  key: string;
  enabled: boolean;
  config: Record<string, unknown>;
}

export interface ModelProfile {
  id: string;
  name: string;
  kind: ModelKind;
  base_url: string;
  model_name: string;
  enabled: boolean;
}

export interface ModelAssignment {
  purpose: string;
  model_profile_id: string;
}

export interface ModelConnectionTestResult {
  healthy: boolean;
  message: string;
  latency_ms: number;
}

export interface RuntimeLog {
  id: string;
  actor_name: string;
  category: "operation" | "error";
  source: string;
  event: string;
  resource_type: string | null;
  resource_id: string | null;
  method: string | null;
  path: string | null;
  details: string | null;
  created_at: string;
  resource_name?: string;
  source_saved?: boolean;
  processing_status?: string;
  page_count?: number;
  related?: { label: string; href: string; kind: "source" | "page" }[];
}

export interface RuntimeLogPage {
  items: RuntimeLog[];
  total: number;
  offset: number;
  limit: number;
}

export interface WikiSchema {
  revision: string;
  content: string;
}

export interface Conflict {
  path: string;
  title: string;
  status: string;
  updated: string;
  sources: string[];
  batch_id?: string | null;
  batch_title?: string | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly detail: unknown
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const entry = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("csrf_token="));
  return entry ? decodeURIComponent(entry.slice("csrf_token=".length)) : undefined;
}

const ACTIVE_WIKI_KEY = "active-personal-wiki-id";

export function getActiveWikiId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(ACTIVE_WIKI_KEY);
}

export function setActiveWikiId(wikiId: string | null): void {
  if (typeof window === "undefined") return;
  if (wikiId) {
    window.localStorage.setItem(ACTIVE_WIKI_KEY, wikiId);
  } else {
    window.localStorage.removeItem(ACTIVE_WIKI_KEY);
  }
}

function errorMessage(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "message" in detail) {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return `API request failed (${status})`;
}

let refreshPromise: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  if (!refreshPromise) {
    const token = csrfToken();
    refreshPromise = fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: token ? { "x-csrf-token": token } : undefined
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  retry = true,
  responseType: "json" | "blob" | "stream" = "json"
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("accept", responseType === "stream" ? "text/event-stream" : "application/json");
  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !headers.has("content-type")
  ) {
    headers.set("content-type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const token = csrfToken();
    if (token) headers.set("x-csrf-token", token);
  }
  const wikiId = getActiveWikiId();
  if (wikiId) headers.set("x-wiki-id", wikiId);

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers
  });

  if (
    response.status === 401 &&
    retry &&
    (!path.startsWith("/auth/") || path === "/auth/me") &&
    (await refreshSession())
  ) {
    return request<T>(path, init, false, responseType);
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      detail = payload.detail;
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(errorMessage(detail, response.status), response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  if (responseType === "blob") return (await response.blob()) as T;
  if (responseType === "stream") {
    if (!response.body) throw new ApiError("实时网页没有返回画面，请重新连接。", 502, null);
    return response.body as T;
  }
  return (await response.json()) as T;
}

const json = (value: unknown) => JSON.stringify(value);
const encodePath = (path: string) =>
  path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");

export const api = {
  captchaChallenge: () =>
    request<CaptchaChallenge>("/auth/captcha/challenge", {}, false),
  verifyCaptcha: (payload: CaptchaVerifyPayload) =>
    request<CaptchaVerification>("/auth/captcha/verify", {
      method: "POST",
      body: json(payload)
    }, false),
  sendPasswordResetCode: (email: string) =>
    request<{ message: string }>("/auth/email-code", {
      method: "POST",
      body: json({ email, purpose: "password_reset" })
    }, false),
  sendEmailBindingCode: (email: string) =>
    request<{ message: string }>("/auth/me/email-code", {
      method: "POST",
      body: json({ email })
    }),
  bindEmail: (email: string, emailCode: string) =>
    request<User>("/auth/me/email", {
      method: "POST",
      body: json({ email, email_code: emailCode })
    }),
  recoverPassword: (payload: {
    email: string;
    password: string;
    email_code: string;
  }) =>
    request<{ message: string }>("/auth/password-recovery", {
      method: "POST",
      body: json(payload)
    }, false),
  login: (username: string, password: string, captchaToken: string) =>
    request<User>("/auth/login", {
      method: "POST",
      body: json({ username, password, captcha_token: captchaToken })
    }, false),
  me: () => request<User>("/auth/me"),
  updateProfile: (displayName: string) =>
    request<User>("/auth/me", {
      method: "PATCH",
      body: json({ display_name: displayName })
    }),
  uploadAvatar: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<User>("/auth/me/avatar", { method: "PUT", body });
  },
  avatarUrl: (version: string) =>
    `${API_BASE_URL}/auth/me/avatar?v=${encodeURIComponent(version)}`,
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ message: string }>("/auth/me/change-password", {
      method: "POST",
      body: json({
        current_password: currentPassword,
        new_password: newPassword
      })
    }),
  deleteAccount: (currentPassword: string) =>
    request<{ message: string }>("/auth/me", {
      method: "DELETE",
      body: json({ current_password: currentPassword })
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }, false),

  listWikis: () => request<PersonalWiki[]>("/wikis"),
  initializeWiki: () => request<PersonalWiki>("/wikis/initialize", { method: "POST" }),
  createWiki: (payload: { name: string; description?: string }) =>
    request<PersonalWiki>("/wikis", {
      method: "POST",
      body: json(payload)
    }),
  updateWiki: (
    wikiId: string,
    payload: { name: string; description?: string }
  ) =>
    request<PersonalWiki>(`/wikis/${encodeURIComponent(wikiId)}`, {
      method: "PATCH",
      body: json(payload)
    }),
  deleteWiki: (wikiId: string) =>
    request<void>(`/wikis/${encodeURIComponent(wikiId)}`, {
      method: "DELETE"
    }),

  listSources: () => request<Source[]>("/sources"),
  uploadSource: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<SourceIngest>("/sources", { method: "POST", body });
  },
  uploadSourceBatch: (files: File[]) => {
    const body = new FormData();
    for (const file of files) body.append("files", file);
    return request<SourceBatchIngest>("/sources/batch", { method: "POST", body });
  },
  compileSourceBatch: (sourceIds: string[]) =>
    request<AgentRun>("/sources/compile-batch", {
      method: "POST",
      body: json({ source_ids: sourceIds })
    }),
  listSourceRevisions: (sourceId: string) =>
    request<SourceRevision[]>(`/sources/${encodeURIComponent(sourceId)}/revisions`),
  getSourceRevision: (revisionId: string) =>
    request<SourceRevision>(`/sources/revisions/${encodeURIComponent(revisionId)}`),
  listSourceSpans: (revisionId: string) =>
    request<SourceSpan[]>(`/sources/revisions/${encodeURIComponent(revisionId)}/spans`),
  readSourceContent: (revisionId: string) =>
    request<Blob>(`/sources/revisions/${encodeURIComponent(revisionId)}/content`, {}, true, "blob"),
  readWebsitePreview: (revisionId: string) =>
    request<Blob>(`/sources/revisions/${encodeURIComponent(revisionId)}/content?preview=true`, {}, true, "blob"),
  retrySource: (sourceId: string) =>
    request<AgentRun>(`/sources/${encodeURIComponent(sourceId)}/retry`, {
      method: "POST"
    }),
  renameSource: (sourceId: string, name: string) =>
    request<Source>(`/sources/${encodeURIComponent(sourceId)}`, {
      method: "PATCH",
      body: json({ name })
    }),
  deleteSource: (sourceId: string) =>
    request<void>(`/sources/${encodeURIComponent(sourceId)}`, {
      method: "DELETE"
    }),

  listWikiPages: () => request<WikiPageSummary>("/wiki/pages"),
  getWikiGraph: () => request<WikiGraph>("/wiki/graph"),
  readWikiPage: (path: string) =>
    request<WikiPage>(`/wiki/pages/${encodePath(path)}`),
  getWikiIndexStatus: () => request<WikiIndexStatus>("/wiki/index/status"),
  rebuildCurrentWikiIndex: () =>
    request<WikiIndexStatus & { commit: string }>("/wiki/index/rebuild", {
      method: "POST"
    }),
  searchWiki: (query: string) =>
    request<WikiSearchResponse>("/wiki/search", {
      method: "POST",
      body: json({ query })
    }),
  askWiki: (query: string, conversationId?: string, requestId?: string) =>
    request<WikiQuestionSubmission>("/questions", {
      method: "POST",
      body: json({
        query,
        ...(conversationId ? { conversation_id: conversationId } : {}),
        ...(requestId ? { request_id: requestId } : {})
      })
    }),
  listQuestionConversations: () =>
    request<WikiQuestionConversationSummary[]>("/questions/conversations"),
  createQuestionConversation: () =>
    request<WikiQuestionConversationSummary>("/questions/conversations", {
      method: "POST"
    }),
  getQuestionConversation: (id: string) =>
    request<WikiQuestionConversation>(`/questions/conversations/${encodeURIComponent(id)}`),
  renameQuestionConversation: (id: string, title: string) =>
    request<WikiQuestionConversationSummary>(`/questions/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: json({ title })
    }),
  deleteQuestionConversation: (id: string) =>
    request<void>(`/questions/conversations/${encodeURIComponent(id)}`, {
      method: "DELETE"
    }),
  getQuestionRun: (id: string, signal?: AbortSignal) =>
    request<WikiQuestionRun>(`/questions/runs/${encodeURIComponent(id)}`, { signal }),
  getQuestionRequest: (requestId: string, signal?: AbortSignal) =>
    request<WikiQuestionRun | null>(`/questions/requests/${encodeURIComponent(requestId)}`, { signal }),
  cancelQuestionRun: (id: string, signal?: AbortSignal) =>
    request<WikiQuestionRun>(`/questions/runs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      signal
    }),
  resumeQuestionRun: (id: string) =>
    request<WikiQuestionSubmission>(`/questions/runs/${encodeURIComponent(id)}/resume`, {
      method: "POST"
    }),
  listFeatures: () => request<FeatureFlag[]>("/features"),

  listDrafts: () => request<Draft[]>("/drafts"),
  publishDraft: (id: string, message = "") =>
    request<Draft>(`/drafts/${encodeURIComponent(id)}/publish`, {
      method: "POST",
      body: json({ message })
    }),
  rejectDraft: (id: string, reason: string) =>
    request<void>(`/drafts/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body: json({ reason })
    }),

  listRuns: () => request<AgentRun[]>("/agent/runs"),
  queueCompilation: (
    sourceRevisionId: string,
    publicationPolicy: "manual_review" | "low_risk_auto_publish"
  ) =>
    request<AgentRun>("/agent/runs/compile", {
      method: "POST",
      body: json({
        source_revision_id: sourceRevisionId,
        model_profile_id: null,
        publication_policy: publicationPolicy
      })
    }),
  queueParse: (sourceRevisionId: string) =>
    request<AgentRun>("/agent/runs/parse", {
      method: "POST",
      body: json({ source_revision_id: sourceRevisionId })
    }),
  queueSourceEmbeddingBackfill: () =>
    request<AgentRun>("/agent/runs/backfill-embeddings", {
      method: "POST"
    }),
  queueCrawl: (payload: {
    url: string;
    max_depth: number;
    max_pages: number;
    include_patterns?: string[];
    exclude_patterns?: string[];
  }) =>
    request<AgentRun>("/agent/runs/crawl", {
      method: "POST",
      body: json(payload)
    }),
  openBrowserSession: (url: string) =>
    request<BrowserSession>("/sources/browser-sessions", { method: "POST", body: json({ url }) }),
  browserSessionFrames: (id: string, signal: AbortSignal) =>
    request<ReadableStream<Uint8Array>>(
      `/sources/browser-sessions/${encodeURIComponent(id)}/frames`, { signal }, true, "stream"
    ),
  browserSessionScreenshot: (id: string) =>
    request<Blob>(`/sources/browser-sessions/${encodeURIComponent(id)}/screenshot`, {}, true, "blob"),
  browserSessionAction: (id: string, action: BrowserAction, signal?: AbortSignal) =>
    request<BrowserSession>(`/sources/browser-sessions/${encodeURIComponent(id)}/actions`, {
      method: "POST", body: json(action), signal
    }),
  importBrowserSession: (id: string) =>
    request<Source>(`/sources/browser-sessions/${encodeURIComponent(id)}/import`, { method: "POST" }),
  closeBrowserSession: (id: string) =>
    request<void>(`/sources/browser-sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),

  listModelProfiles: () => request<ModelProfile[]>("/models"),
  createModelProfile: (payload: {
    name: string;
    kind: ModelKind;
    base_url: string;
    model_name: string;
    api_key: string;
    enabled: boolean;
  }) => request<ModelProfile>("/models", { method: "POST", body: json(payload) }),
  updateModelProfile: (
    id: string,
    payload: {
      name: string;
      base_url: string;
      model_name: string;
      api_key: string | null;
      enabled: boolean;
    }
  ) =>
    request<ModelProfile>(`/models/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: json(payload)
    }),
  testModelConnection: (payload: {
    profile_id: string | null;
    base_url: string;
    model_name: string;
    api_key: string | null;
  }) =>
    request<ModelConnectionTestResult>("/models/test", {
      method: "POST",
      body: json(payload)
    }),
  deleteModelProfile: (id: string) =>
    request<void>(`/models/${encodeURIComponent(id)}`, { method: "DELETE" }),
  listModelAssignments: () => request<ModelAssignment[]>("/models/assignments"),
  updateModelAssignment: (purpose: string, modelProfileId: string) =>
    request<ModelAssignment>(
      `/models/assignments/${encodeURIComponent(purpose)}`,
      {
        method: "PUT",
        body: json({ model_profile_id: modelProfileId })
      }
    ),

  rebuildWikiIndex: () =>
    request<{ commit: string }>("/wiki/index/rebuild", { method: "POST" }),
  listLogs: (filters: {
    category: "all" | "operation" | "error";
    source: string;
    dateFrom: string;
    dateTo: string;
    search: string;
    offset: number;
    limit: number;
  }) => {
    const query = new URLSearchParams({
      category: filters.category,
      source: filters.source,
      offset: String(filters.offset),
      limit: String(filters.limit)
    });
    if (filters.dateFrom) query.set("date_from", filters.dateFrom);
    if (filters.dateTo) query.set("date_to", filters.dateTo);
    if (filters.search.trim()) query.set("search", filters.search.trim());
    return request<RuntimeLogPage>(`/logs?${query.toString()}`);
  },
  readSchema: () => request<WikiSchema>("/wiki/schema"),
  updateSchema: (baseCommit: string, content: string) =>
    request<WikiSchema>("/wiki/schema", {
      method: "PUT",
      body: json({ base_commit: baseCommit, content })
    }),
  listConflicts: () => request<Conflict[]>("/conflicts")
};
