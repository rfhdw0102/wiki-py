import assert from "node:assert/strict";
import test from "node:test";
import {
  logDescription, logExternalLink, logFileInfo, logOperation, logTechnicalDetails
} from "../src/app/logs/log-description.ts";
import { importFailureMessage } from "../src/app/wiki/source-processing.ts";

const entry = (changes = {}) => ({
  id: "log", actor_name: "用户", category: "operation", source: "原件管理",
  event: "source.uploaded", resource_type: "source_revision", resource_id: "revision",
  method: null, path: null, details: null, created_at: "2026-09-30T08:00:00Z",
  ...changes
});

test("published paths become a page count, not file paths or opaque IDs", () => {
  const log = entry({ event: "draft.published", page_count: 4,
    details: JSON.stringify({ paths: ["entities/agent.md", "sources/private-uuid.md"], commit: "hash" }) });
  assert.equal(logDescription(log), "已完成发布，涉及 4 个知识页面。");
  assert.ok(!logDescription(log).includes("private-uuid"));
  assert.equal(logTechnicalDetails(log)[0].label, "变更路径");
  assert.match(logDescription(entry({ event: "draft.created", details: '{"title":"新知识草稿"}' })), /等待审核/);
  assert.ok(!logDescription(entry({ event: "draft.created" })).includes("已发布"));
});

test("file records use human names and readable size without promising unverified compilation", () => {
  const log = entry({ details: JSON.stringify({ name: "说明.md", media_type: "text/markdown", size_bytes: 16908 }) });
  assert.equal(logDescription(log), "已保存《说明.md》为原件。");
  assert.equal(logFileInfo(log), "Markdown · 16.5 KB");
  assert.ok(!logDescription(log).includes("正在整理"));
  assert.notEqual(logDescription(entry({ event: "auth.login" })), "—");
});

test("failure descriptions name the real operation and do not guess saved/published state", () => {
  const failed = entry({ category: "error", resource_type: "api_request", event: "HTTPError",
    path: "/api/v1/sources/browser-sessions/session/import", method: "POST", details: "API returned HTTP 500" });
  assert.equal(logOperation(failed), "保存网页");
  assert.match(logDescription(failed), /保存结果尚未确认/);
  assert.ok(!logDescription(failed).includes("未保存"));
  assert.match(logDescription(entry({ ...failed, path: "/api/v1/auth/refresh" })), /重新登录/);
  assert.match(logDescription(entry({ ...failed, path: "/api/v1/wiki/drafts/draft/publish" })), /核对/);
  assert.match(logDescription(entry({ ...failed, resource_type: "agent_run", processing_status: "published_index_failed" })), /不需要重复发布/);
});

test("known compilation rules reuse safe diagnostics but unknown errors never echo secrets", () => {
  const known = entry({ category: "error", event: "compile_source", resource_type: "agent_run",
    details: "No compilation model is assigned" });
  assert.match(logDescription(known, importFailureMessage), /模型/);
  const unknown = entry({ ...known, details: "SECRET_API_KEY raw response body" });
  assert.ok(!JSON.stringify([logDescription(unknown), logTechnicalDetails(unknown)]).includes("SECRET_API_KEY"));
});

test("external links remain usable without credential-bearing or unsafe URLs", () => {
  const log = entry({ event: "source.web_added",
    details: '{"name":"文章","url":"https://example.com/article?id=1&access_token=SECRET&code=SECRET#anchor"}' });
  assert.deepEqual(logExternalLink(log), { href: "https://example.com/article?id=1", label: "example.com" });
  for (const url of ["javascript:alert(1)", "https://user:password@example.com"]) {
    assert.equal(logExternalLink(entry({ details: JSON.stringify({ url }) })), null);
  }
  assert.ok(logTechnicalDetails(entry({ details: '{"password":"SECRET","api_key":"SECRET"}' })).length === 0);
});
