import type { api } from "@wiki-agent/web-ui";

export type Catalog = Awaited<ReturnType<typeof api.listDingtalkDocuments>>;
export type ImportRequest = Parameters<typeof api.importDingtalkDocument>[0];
export type ImportStatus = Awaited<ReturnType<typeof api.readDingtalkImport>>;
export type ImportOutcome = Partial<ImportStatus> & {
  request_id: string;
  message?: string;
  wiki_readable: boolean;
};

type ImportApi = Pick<typeof api, "importDingtalkDocument" | "readDingtalkImport" | "readWikiPage">;

export async function loadDingtalkCatalog(
  client: Pick<typeof api, "dingtalkStatus" | "listDingtalkDocuments">
) {
  const status = await client.dingtalkStatus();
  const catalog = status.mode === "mock" ? await client.listDingtalkDocuments() : null;
  return { status, catalog };
}

export function createImportRequest(
  catalog: Catalog,
  documentId: string | null,
  generateId: () => string
): ImportRequest | null {
  const selected = catalog.items.find((item) => item.document_id === documentId);
  if (!selected) return null;
  return { document_id: selected.document_id, request_id: generateId() };
}

export function wikiPathLink(path: string) {
  return `/wiki?path=${encodeURIComponent(path)}`;
}

export function publicationNotice(outcome: ImportOutcome) {
  if (outcome.state === "published" && outcome.wiki_readable) {
    return "模拟导入完成；已通过正常 Wiki 接口读取页面。";
  }
  if (outcome.published === true) return "页面已发布但状态记录失败或尚未完成核验";
  if (outcome.published === null || outcome.state === "outcome_unknown") return "结果待核验";
  if (outcome.state === "failed" && outcome.published === false) return "导入失败，尚未发布";
  return "结果待核验；请只读查询本次动作，不要重新导入。";
}

function importFailure(cause: unknown, previous: ImportOutcome): ImportOutcome {
  const error = cause as { message?: unknown; detail?: unknown } | null;
  const detail = error?.detail as Record<string, unknown> | null;
  const next = { ...previous, wiki_readable: false };
  next.message = typeof error?.message === "string" ? error.message : "请求失败；请查询本次导入结果。";
  if (detail && typeof detail === "object") {
    if (typeof detail.page_path === "string") next.page_path = detail.page_path;
    if (typeof detail.published_commit === "string") next.published_commit = detail.published_commit;
    if (detail.published === true || detail.published === false || detail.published === null) {
      next.published = detail.published;
    }
  }
  // A failed check must not retain an earlier 'published' state as a success signal.
  if (next.published === null) next.state = "outcome_unknown";
  else if (next.published === false) next.state = "failed";
  // The clicked action's ID is authoritative, including on transport errors.
  return next;
}

async function verifyNormalRead(
  status: ImportStatus,
  previous: ImportOutcome,
  client: ImportApi
): Promise<ImportOutcome> {
  const next: ImportOutcome = {
    ...previous,
    ...status,
    request_id: previous.request_id,
    page_path: status.page_path ?? previous.page_path,
    published_commit: status.published_commit ?? previous.published_commit,
    wiki_readable: false,
    message: undefined
  };
  if (status.state !== "published") return next;
  next.published = true;
  if (!status.page_path || !status.page_id || !status.published_commit) {
    return { ...next, message: "已发布响应缺少页面标识；请查询核验。" };
  }
  try {
    const page = await client.readWikiPage(status.page_path);
    if (page.metadata.id !== status.page_id) {
      return { ...next, message: "正常 Wiki 页面标识不匹配；请查询核验。" };
    }
    return { ...next, wiki_readable: true };
  } catch (cause) {
    return importFailure(cause, next);
  }
}

export async function submitImport(request: ImportRequest, client: ImportApi): Promise<ImportOutcome> {
  const initial: ImportOutcome = { request_id: request.request_id, wiki_readable: false };
  try {
    return await verifyNormalRead(await client.importDingtalkDocument(request), initial, client);
  } catch (cause) {
    return importFailure(cause, initial);
  }
}

export async function checkImport(previous: ImportOutcome, client: ImportApi): Promise<ImportOutcome> {
  try {
    return await verifyNormalRead(await client.readDingtalkImport(previous.request_id), previous, client);
  } catch (cause) {
    return importFailure(cause, previous);
  }
}
