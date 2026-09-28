declare const process: { env: Record<string, string | undefined> };

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1"
).replace(/\/+$/, "");

export type UserRole = "admin" | "user";
export type ModelKind = "chat" | "embedding" | "reranker";

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
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

export interface SourceSpan {
  id: string;
  source_revision_id: string;
  ordinal: number;
  heading: string | null;
  content: string;
  locator: Record<string, unknown>;
}

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

export interface WikiSearchHit {
  document_id: string;
  score: number;
  channels: string[];
  content: string;
  metadata: Record<string, unknown>;
}

export interface WikiSearchResponse {
  query: string;
  layer: string;
  fallback_used: boolean;
  channels: string[];
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
  evidence: Array<{
    document_id: string;
    channels: string[];
    metadata: Record<string, unknown>;
  }>;
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
  config: Record<string, unknown>;
}

export interface ModelAssignment {
  purpose: string;
  model_profile_id: string;
}

export interface AdminJob {
  id: string;
  kind: string;
  status: string;
  created_by_id: string;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface AuditEntry {
  id: string;
  actor_id: string | null;
  action: string;
  resource_type: string;
  resource_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
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
}

export interface KnowledgeGap {
  query: string;
  occurrences: number;
  last_seen: string;
  best_hit_count: number;
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
  retry = true
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
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

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers
  });

  if (
    response.status === 401 &&
    retry &&
    path !== "/auth/login" &&
    path !== "/auth/refresh" &&
    (await refreshSession())
  ) {
    return request<T>(path, init, false);
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
  return (await response.json()) as T;
}

const json = (value: unknown) => JSON.stringify(value);
const encodePath = (path: string) =>
  path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");

export const api = {
  login: (email: string, password: string) =>
    request<User>("/auth/login", {
      method: "POST",
      body: json({ email, password })
    }),
  me: () => request<User>("/auth/me"),
  logout: () => request<void>("/auth/logout", { method: "POST" }, false),

  listSources: () => request<Source[]>("/sources"),
  uploadSource: (file: File) => {
    const body = new FormData();
    body.append("file", file);
    return request<SourceRevision>("/sources", { method: "POST", body });
  },
  listSourceRevisions: (sourceId: string) =>
    request<SourceRevision[]>(`/sources/${encodeURIComponent(sourceId)}/revisions`),
  listSourceSpans: (revisionId: string) =>
    request<SourceSpan[]>(`/sources/revisions/${encodeURIComponent(revisionId)}/spans`),

  listWikiPages: () => request<WikiPageSummary>("/wiki/pages"),
  readWikiPage: (path: string) =>
    request<WikiPage>(`/wiki/pages/${encodePath(path)}`),
  searchWiki: (query: string) =>
    request<WikiSearchResponse>("/wiki/search", {
      method: "POST",
      body: json({ query })
    }),
  askWiki: (query: string) =>
    request<WikiQuestionResponse>("/questions", {
      method: "POST",
      body: json({ query })
    }),
  listFeatures: () => request<FeatureFlag[]>("/features"),

  listDrafts: () => request<Draft[]>("/drafts"),
  publishDraft: (id: string, message: string) =>
    request<Draft>(`/drafts/${encodeURIComponent(id)}/publish`, {
      method: "POST",
      body: json({ message })
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

  listUsers: () => request<User[]>("/admin/users"),
  createUser: (payload: {
    email: string;
    password: string;
    display_name: string;
    role: UserRole;
  }) => request<User>("/admin/users", { method: "POST", body: json(payload) }),
  updateUserStatus: (id: string, isActive: boolean) =>
    request<User>(`/admin/users/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: json({ is_active: isActive })
    }),
  resetUserPassword: (id: string, password: string) =>
    request<void>(`/admin/users/${encodeURIComponent(id)}/reset-password`, {
      method: "POST",
      body: json({ password })
    }),
  updateSourceTrust: (id: string, trustLevel: number) =>
    request<Source>(`/admin/sources/${encodeURIComponent(id)}/trust`, {
      method: "PUT",
      body: json({ trust_level: trustLevel })
    }),

  listFeatureFlags: () => request<FeatureFlag[]>("/admin/features"),
  updateFeatureFlag: (key: string, enabled: boolean, config: Record<string, unknown>) =>
    request<FeatureFlag>(`/admin/features/${encodeURIComponent(key)}`, {
      method: "PUT",
      body: json({ enabled, config })
    }),

  listModelProfiles: () => request<ModelProfile[]>("/admin/models"),
  createModelProfile: (payload: {
    name: string;
    kind: ModelKind;
    base_url: string;
    model_name: string;
    api_key: string;
    enabled: boolean;
    config: Record<string, unknown>;
  }) => request<ModelProfile>("/admin/models", { method: "POST", body: json(payload) }),
  listModelAssignments: () =>
    request<ModelAssignment[]>("/admin/model-assignments"),
  updateModelAssignment: (purpose: string, modelProfileId: string) =>
    request<ModelAssignment>(
      `/admin/model-assignments/${encodeURIComponent(purpose)}`,
      {
        method: "PUT",
        body: json({ model_profile_id: modelProfileId })
      }
    ),

  listAdminJobs: () => request<AdminJob[]>("/admin/jobs"),
  rebuildWikiIndex: () =>
    request<{ commit: string }>("/admin/jobs/rebuild-index", { method: "POST" }),
  listAuditEntries: () => request<AuditEntry[]>("/admin/audit"),
  listKnowledgeGaps: () => request<KnowledgeGap[]>("/knowledge-gaps"),
  readSchema: () => request<WikiSchema>("/admin/schema"),
  updateSchema: (baseCommit: string, content: string, message: string) =>
    request<WikiSchema>("/admin/schema", {
      method: "PUT",
      body: json({ base_commit: baseCommit, content, message })
    }),
  listConflicts: () => request<Conflict[]>("/conflicts")
};
