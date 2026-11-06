import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { api, ApiError } from "../../../packages/web-ui/src/api.ts";
import {
  checkImport, createImportRequest, loadDingtalkCatalog, publicationNotice,
  submitImport, wikiPathLink
} from "../src/app/dingtalk/import.ts";
import { simulationBadge } from "../src/app/wiki/simulation.ts";
import { runKindLabel } from "../src/app/tasks/run-kind.ts";

const requestId = "bb0fd8a8-41b0-4c79-bc99-4cc022ba3a4d";
const catalog = {
  mode: "mock", simulation: true, notice: "模拟钉钉，未连接真实钉钉",
  fixture_version: "fixture-v1",
  items: [
    { document_id: "mock-a", title: "模拟文档A：项目说明" },
    { document_id: "mock-b", title: "模拟文档B：会议记录" }
  ]
};
const published = {
  request_id: requestId, state: "published", mode: "mock", simulation: true,
  real_connection: "not_implemented", page_path: `sources/dingtalk-mock-${requestId}.md`,
  page_id: `sim-dt-${requestId}`, published_commit: "known-git-commit",
  source_revision_id: "source-revision", index_status: "deferred"
};
const page = {
  metadata: {
    id: published.page_id, title: "模拟文档A：项目说明",
    tags: ["dingtalk-mock", "simulation"]
  },
  body: "这是模拟文档 A 的第一段，说明本地导入流程。English: Alpha only.\n\n这是模拟文档 A 的第二段，保留中文、English 和标点；顺序不得改变。\n\n来源：模拟钉钉（未连接真实钉钉）。 [^sim-1] [^sim-2]"
};
const client = (overrides = {}) => ({
  importDingtalkDocument: async () => published,
  readDingtalkImport: async () => published,
  readWikiPage: async () => page,
  ...overrides
});

// V-SIM01/02, V-SAFE02: server mode/catalog, not a browser feature toggle.
test("disabled status does not fetch a mock catalog or pretend a real connection", async () => {
  let listed = false;
  const result = await loadDingtalkCatalog({
    dingtalkStatus: async () => ({ mode: "disabled", simulation: false, real_connection: "not_implemented", fixture_version: null }),
    listDingtalkDocuments: async () => { listed = true; return catalog; }
  });
  assert.equal(listed, false);
  assert.equal(result.catalog, null);
  assert.equal(result.status.real_connection, "not_implemented");
});

test("mock catalog is loaded from the server after status", async () => {
  const calls = [];
  const result = await loadDingtalkCatalog({
    dingtalkStatus: async () => { calls.push("status"); return { mode: "mock" }; },
    listDingtalkDocuments: async () => { calls.push("documents"); return catalog; }
  });
  assert.deepEqual(calls, ["status", "documents"]);
  assert.equal(result.catalog, catalog);
});

test("unselected/unknown choices create no action; A and B produce exactly one selected ID", () => {
  let ids = 0;
  const generateId = () => { ids += 1; return requestId; };
  assert.equal(createImportRequest(catalog, null, generateId), null);
  assert.equal(createImportRequest(catalog, "unknown", generateId), null);
  assert.equal(ids, 0);
  assert.deepEqual(createImportRequest(catalog, "mock-a", generateId), { document_id: "mock-a", request_id: requestId });
  assert.deepEqual(createImportRequest(catalog, "mock-b", generateId), { document_id: "mock-b", request_id: requestId });
  assert.equal(ids, 2);
});

// V-ONCE01, V-SAFE01: exercise the actual shared HTTP client, not a mock serializer.
test("401 refresh replays identical selected B JSON/UUID with cookies and CSRF", async (t) => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  t.after(() => {
    globalThis.fetch = originalFetch;
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else delete globalThis.document;
  });
  Object.defineProperty(globalThis, "document", { configurable: true, value: { cookie: "csrf_token=test%20token" } });
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (calls.length === 1) return new Response(JSON.stringify({ detail: "expired" }), { status: 401 });
    if (url.endsWith("/auth/refresh")) return new Response("{}", { status: 200 });
    return new Response(JSON.stringify(published), { status: 200 });
  };
  await api.importDingtalkDocument({ document_id: "mock-b", request_id: requestId });
  assert.equal(calls.length, 3);
  assert.ok(calls[0].url.endsWith("/integrations/dingtalk/imports"));
  assert.ok(calls[1].url.endsWith("/auth/refresh"));
  assert.equal(calls[0].init.body, calls[2].init.body);
  assert.deepEqual(JSON.parse(calls[2].init.body), { document_id: "mock-b", request_id: requestId });
  for (const { init } of calls) {
    assert.equal(init.credentials, "include");
    assert.equal(new Headers(init.headers).get("x-csrf-token"), "test token");
  }
});

test("shared status/catalog/history wrappers use authenticated GET without import writes", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return new Response("{}", { status: 200 });
  };
  await api.dingtalkStatus();
  await api.listDingtalkDocuments();
  await api.readDingtalkImport(requestId);
  assert.deepEqual(calls.map(({ url }) => new URL(url).pathname), [
    "/api/v1/integrations/dingtalk/status",
    "/api/v1/integrations/dingtalk/documents",
    `/api/v1/integrations/dingtalk/imports/${requestId}`
  ]);
  for (const { init } of calls) {
    assert.equal(init.method ?? "GET", "GET");
    assert.equal(init.body, undefined);
    assert.equal(init.credentials, "include");
  }
});

// V-SIM04: only published state PLUS a normal Wiki read yields completion.
test("published result requires normal Wiki GET and produces a normal path link", async () => {
  const readPaths = [];
  const outcome = await submitImport({ document_id: "mock-a", request_id: requestId }, client({
    readWikiPage: async (path) => { readPaths.push(path); return page; }
  }));
  assert.deepEqual(readPaths, [published.page_path]);
  assert.equal(outcome.wiki_readable, true);
  assert.match(publicationNotice(outcome), /模拟导入完成/);
  assert.equal(wikiPathLink(published.page_path), `/wiki?path=${encodeURIComponent(published.page_path)}`);
});

test("mode mock/index deferred alone do not constitute published success", async () => {
  let reads = 0;
  const outcome = await submitImport({ document_id: "mock-a", request_id: requestId }, client({
    importDingtalkDocument: async () => ({ ...published, state: "prepared" }),
    readWikiPage: async () => { reads += 1; return page; }
  }));
  assert.equal(reads, 0);
  assert.equal(outcome.wiki_readable, false);
  assert.doesNotMatch(publicationNotice(outcome), /导入完成/);
});

test("unreadable/mismatched/missing normal page never claims complete success", async () => {
  for (const overrides of [
    { readWikiPage: async () => { throw new Error("unreadable"); } },
    { readWikiPage: async () => ({ ...page, metadata: { ...page.metadata, id: "other-id" } }) },
    { importDingtalkDocument: async () => ({ ...published, page_id: null }) }
  ]) {
    const outcome = await submitImport({ document_id: "mock-a", request_id: requestId }, client(overrides));
    assert.equal(outcome.wiki_readable, false);
    assert.equal(outcome.page_path, published.page_path);
    assert.equal(outcome.published_commit, published.published_commit);
    assert.doesNotMatch(publicationNotice(outcome), /导入完成/);
  }
});

// V-STATE01: known publication and uncertainty must not suggest a new action.
for (const knownPublished of [true, null]) {
  test(`published=${knownPublished} errors preserve action/path/commit and GET checking never imports again`, async () => {
    let imports = 0;
    let checks = 0;
    const failure = new ApiError("固定脱敏说明", 503, {
      code: knownPublished ? "published_state_not_recorded" : "publication_outcome_unknown",
      published: knownPublished, page_path: published.page_path,
      published_commit: published.published_commit, request_id: requestId
    });
    const mocked = client({
      importDingtalkDocument: async () => { imports += 1; throw failure; },
      readDingtalkImport: async (id) => {
        checks += 1;
        assert.equal(id, requestId);
        return { request_id: id, state: "outcome_unknown", published: knownPublished };
      }
    });
    const outcome = await submitImport({ document_id: "mock-a", request_id: requestId }, mocked);
    assert.equal(outcome.request_id, requestId);
    assert.equal(outcome.page_path, published.page_path);
    assert.equal(outcome.published_commit, published.published_commit);
    assert.equal(outcome.published, knownPublished);
    assert.match(publicationNotice(outcome), knownPublished ? /页面已发布但状态记录失败/ : /结果待核验/);
    const checked = await checkImport(outcome, mocked);
    assert.equal(imports, 1);
    assert.equal(checks, 1);
    assert.equal(checked.request_id, requestId);
    assert.equal(checked.page_path, published.page_path);
    assert.equal(checked.published_commit, published.published_commit);
    assert.equal(checked.wiki_readable, false);
  });
}

test("lost response retains UUID, and a failed GET retains previously known publication", async () => {
  const outcome = await submitImport({ document_id: "mock-a", request_id: requestId }, client({
    importDingtalkDocument: async () => { throw new TypeError("Failed to fetch"); }
  }));
  assert.equal(outcome.request_id, requestId);
  assert.match(publicationNotice(outcome), /不要重新导入/);
  const checked = await checkImport({ ...outcome, ...published, published: true }, client({
    readDingtalkImport: async () => { throw new Error("query unavailable"); }
  }));
  assert.equal(checked.published, true);
  assert.equal(checked.published_commit, published.published_commit);
  assert.equal(checked.page_path, published.page_path);
});

test("read-only check can confirm published state via normal Wiki read", async () => {
  const outcome = await checkImport({ request_id: requestId, wiki_readable: false, published: null }, client({
    importDingtalkDocument: async () => { throw new Error("must not reimport"); }
  }));
  assert.equal(outcome.wiki_readable, true);
  assert.equal(outcome.request_id, requestId);
  assert.match(publicationNotice(outcome), /模拟导入完成/);
});

// V-REG01: tags add a badge only; title/body rendering remains normal.
test("either simulation tag adds a warning badge; old untagged pages remain unchanged", async () => {
  assert.equal(simulationBadge(["dingtalk-mock"]), "模拟钉钉 · 未连接真实钉钉");
  assert.equal(simulationBadge(["simulation"]), "模拟钉钉 · 未连接真实钉钉");
  assert.equal(simulationBadge(["existing-tag"]), null);
  assert.equal(simulationBadge([]), null);
  const source = await readFile(new URL("../src/app/wiki/page.tsx", import.meta.url), "utf8");
  assert.match(source, /simulationBadge\(page\.metadata\.tags\)/);
  assert.match(source, /<h2>\{page\.metadata\.title\}<\/h2>/);
  assert.match(source, /<div className="wiki-body">\{page\.body\}<\/div>/);
  assert.match(source, /api\.readWikiPage\(path\)/);
});

test("UI binds server radios without a default and keeps UUID-producing import disabled after an attempt", async () => {
  const source = await readFile(new URL("../src/app/dingtalk/page.tsx", import.meta.url), "utf8");
  assert.match(source, /useState<string \| null>\(null\)/);
  assert.match(source, /data\.catalog\.items\.map/);
  assert.match(source, /checked=\{selectedDocument === item\.document_id\}/);
  assert.match(source, /name="dingtalk-document"/);
  assert.match(source, /type="radio"/);
  assert.match(source, /disabled=\{busy \|\| !selectedDocument \|\| outcome !== null\}/);
  assert.match(source, /actionInFlight\.current \|\| outcome/);
  assert.match(source, /outcome\.state === "published" && outcome\.wiki_readable/);
  assert.match(source, /模拟功能未启用；真实钉钉尚未实现/);
  const nav = await readFile(new URL("../src/app/layout.tsx", import.meta.url), "utf8");
  assert.match(nav, /href: "\/dingtalk", label: "模拟钉钉导入"/);
});

test("task queue labels the synchronous mock kind and has no generic resume/cancel actions", async () => {
  assert.equal(runKindLabel("dingtalk_mock_import"), "模拟钉钉导入");
  assert.equal(runKindLabel("compile_source"), "compile_source");
  const source = await readFile(new URL("../src/app/tasks/page.tsx", import.meta.url), "utf8");
  assert.match(source, /runKindLabel\(String\(value\)\)/);
  assert.doesNotMatch(source, /api\.(resume|restore|cancel)Run/);
});
