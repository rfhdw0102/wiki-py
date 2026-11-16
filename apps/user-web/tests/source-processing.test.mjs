import assert from "node:assert/strict";
import test from "node:test";
import {
  crawlFailureMessage,
  batchIngestResult,
  canRetrySource,
  hasVisibleSourceForRun,
  importFailureMessage,
  ingestResult,
  isActiveImportRun,
  sourceProcessingStatus,
  sourceRunRevisionIds,
  sourceIsIdleForCompilation
} from "../src/app/wiki/source-processing.ts";

const source = {
  id: "source-1",
  name: "manual.md",
  kind: "markdown",
  current_revision_id: "revision-1"
};

function compilation(status, error = null, result_json = null) {
  return {
    id: `run-${status}`,
    kind: "compile_source",
    status,
    error,
    result_json,
    input_json: { source_revision_id: "revision-1" }
  };
}

test("a saved revision is never shown as published without a completed compilation", () => {
  assert.equal(sourceProcessingStatus(source, [], []).label, "未编译");
  assert.equal(
    sourceProcessingStatus({ ...source, kind: "website" }, [], []).detail,
    "网页已采集，但尚未自动编译为 Wiki 页面。"
  );
  assert.equal(sourceProcessingStatus(source, [compilation("queued")], []).label, "等待处理");
  assert.equal(
    sourceProcessingStatus(source, [compilation("running")], []).label,
    "编译中"
  );
  assert.equal(sourceProcessingStatus(source, [compilation("completed")], []).label, "待确认");
  assert.equal(
    sourceProcessingStatus(source, [
      compilation("completed", null, { published_commit: "abc123" })
    ], []).label,
    "已发布"
  );
});

test("one shared compilation run updates status and eligibility for every batch member", () => {
  const members = [source, { ...source, id: "source-2", current_revision_id: "revision-2" }];
  const run = {
    ...compilation("queued"),
    input_json: {
      batch: true, source_revision_id: "revision-1",
      source_revision_ids: ["revision-1", "revision-2", "revision-2", null, 42]
    }
  };
  assert.deepEqual(sourceRunRevisionIds(run), ["revision-1", "revision-2"]);
  for (const status of ["queued", "parsing", "running", "awaiting_review", "completed", "published_index_failed", "failed"]) {
    const shared = { ...run, status, result_json: { draft_id: "batch-draft" } };
    assert.deepEqual(sourceProcessingStatus(members[0], [shared], []), sourceProcessingStatus(members[1], [shared], []));
    for (const member of members) {
      assert.equal(sourceIsIdleForCompilation(member, [shared], []), status === "failed");
    }
  }
  assert.equal(hasVisibleSourceForRun(run, [members[1]]), true);
  assert.equal(hasVisibleSourceForRun(run, [{ ...source, current_revision_id: "unrelated" }]), false);
  const reviewing = { ...run, status: "awaiting_review", result_json: { draft_id: "batch-draft" } };
  for (const member of members) {
    assert.equal(sourceProcessingStatus(member, [reviewing], [{ id: "batch-draft", status: "published" }]).label, "已发布");
    assert.equal(sourceIsIdleForCompilation(member, [reviewing], [{ id: "batch-draft", status: "rejected" }]), true);
  }
  assert.deepEqual(sourceRunRevisionIds({ ...run, input_json: { source_revision_id: "legacy" } }), ["legacy"]);
  assert.deepEqual(sourceRunRevisionIds({ ...run, input_json: { source_revision_ids: ["list-only"] } }), ["list-only"]);
});

test("batch ingest accepts one common run while preserving per-file errors", () => {
  const revision = { compilation_run_id: "shared", compilation_status: "queued" };
  const response = {
    compilation_run_id: "shared", compilation_status: "queued", compilation_error: null,
    items: [
      { file_name: "one.md", revision, error: null },
      { file_name: "two.txt", revision, error: null },
      { file_name: "invalid.pdf", revision: null, error: "无法解析 PDF" }
    ]
  };
  assert.deepEqual(batchIngestResult(response), {
    saved: 2, queued: 2, runId: "shared", failures: ["invalid.pdf：无法解析 PDF"]
  });
  for (const invalid of [
    { ...response, compilation_run_id: null },
    { ...response, compilation_status: "unknown" },
    { ...response, compilation_status: "failed", compilation_error: "Queue submission failed" },
    { ...response, items: [{ file_name: "one.md", revision: { ...revision, compilation_run_id: "independent" }, error: null }] }
  ]) {
    const result = batchIngestResult(invalid);
    assert.equal(result.queued, 0);
    assert.equal(result.runId, null);
    assert.match(result.failures.join("\n"), /整批编译未成功提交/);
  }
  assert.equal(batchIngestResult({ ...response, items: [response.items[2]] }).queued, 0);
  const incomplete = batchIngestResult({ ...response, items: [response.items[0]] }, 2);
  assert.equal(incomplete.queued, 0);
  assert.match(incomplete.failures.join("\n"), /未返回全部文件/);
  const empty = batchIngestResult({ ...response, items: [] }, 2);
  assert.equal(empty.queued, 0);
  assert.match(empty.failures.join("\n"), /整批导入状态无法确认/);
});

test("failed jobs show actionable messages without exposing raw internal errors", () => {
  const missingModel = "No compilation model is assigned and model_profile_id was not provided";
  const status = sourceProcessingStatus(source, [compilation("failed", missingModel)], []);
  assert.equal(status.label, "处理失败");
  assert.equal(status.tone, "danger");
  assert.match(status.detail, /知识编译模型/);
  assert.doesNotMatch(status.detail, /model_profile_id/);
  assert.deepEqual(status.action, { href: "/models", label: "配置编译模型 →" });
  assert.match(importFailureMessage("Queue submission failed: connection refused"), /后台任务/);
  assert.match(
    importFailureMessage("OpenAIInvalidRequestError: Error code: 400 - This response_format type is unavailable now"),
    /兼容格式/
  );
  assert.match(
    importFailureMessage("OpenAIInvalidRequestError: Thinking mode does not support this tool_choice"),
    /思考模式不接受强制工具选择/
  );
  assert.match(
    importFailureMessage("OpenAIInvalidRequestError: tool_choice is unsupported"),
    /工具调用参数/
  );
  assert.match(
    importFailureMessage("CompilationOutputError: The compilation model returned Markdown pages without required YAML frontmatter and citations."),
    /缺少必需的 YAML 元数据或来源引用/
  );
  assert.match(
    importFailureMessage("OutputParserException: Failed to parse. Got: Wiki page must start with YAML frontmatter"),
    /缺少必需的 YAML 元数据或来源引用/
  );
  assert.match(
    importFailureMessage("CompilationOutputError: The compilation model returned incomplete or invalid Wiki page metadata."),
    /Wiki 页标题或元数据不完整/
  );
  assert.match(
    importFailureMessage("OpenAITimeoutError: Request timed out."),
    /知识处理中的模型调用超时/
  );
  assert.equal(
    sourceProcessingStatus(source, [compilation("published_index_failed")], []).label,
    "索引失败"
  );
  assert.match(crawlFailureMessage(null), /网站采集失败/);
});

test("a successful upload response with a failed compilation run is an error, not a success", () => {
  const response = {
    compilation_run_id: "run-1",
    compilation_status: "failed",
    compilation_error: "No compilation model is assigned and model_profile_id was not provided"
  };
  assert.deepEqual(ingestResult(response), {
    queued: false,
    detail: importFailureMessage(response.compilation_error)
  });

  assert.deepEqual(ingestResult({ ...response, compilation_status: "queued" }), {
    queued: true
  });
  assert.equal(ingestResult({ ...response, compilation_status: "unknown" }).queued, false);
});

test("validation failures explain the rejected rule in user-facing language", () => {
  const cases = [
    ["Concept pages require at least two independent sources.", /至少两份独立来源/],
    ["Pending pages cannot have published status.", /待审核状态/],
    ["Wiki pages contain unsafe HTML or a dangerous link scheme.", /不安全的 HTML/],
    ["The compilation model returned duplicate page paths.", /相同路径/],
    ["The compilation model returned an invalid Wiki page path.", /命名或目录规则/],
    ["The compilation model returned invalid fields: summary.", /处理摘要.*长度要求/]
  ];
  for (const [reason, expected] of cases) {
    assert.match(importFailureMessage(`CompilationOutputError: ${reason}`), expected);
  }
});

test("manual review and published drafts are not confused with a completed import", () => {
  const run = compilation("awaiting_review", null, { draft_id: "draft-1" });
  assert.equal(sourceProcessingStatus(source, [run], []).label, "待审核");
  assert.deepEqual(
    sourceProcessingStatus(source, [run], [{ id: "draft-1", status: "open" }]).action,
    { href: "/conflicts", label: "查看待审核草稿 →" }
  );
  assert.equal(
    sourceProcessingStatus(source, [run], [{ id: "draft-1", status: "published" }]).label,
    "已发布"
  );
  assert.equal(
    sourceProcessingStatus(source, [run], [{ id: "draft-1", status: "rejected" }]).label,
    "未通过"
  );
});

test("concept validation errors describe page declarations, not the number of uploads", () => {
  const message = importFailureMessage(
    "CompilationOutputError: Concept pages require at least two independent sources."
  );
  assert.match(message, /声明/);
  assert.match(message, /不代表只上传了一份/);
  assert.match(message, /重试/);
});

test("polling continues until import runs reach a terminal state", () => {
  assert.equal(isActiveImportRun(compilation("queued")), true);
  assert.equal(isActiveImportRun(compilation("publishing")), true);
  assert.equal(isActiveImportRun(compilation("completed")), false);
  assert.equal(isActiveImportRun(compilation("failed")), false);
  assert.equal(isActiveImportRun({ ...compilation("running"), kind: "crawl_site" }), true);
});

test("only failed or uncompiled sources offer retry, never published or indexing failures", () => {
  const status = (label) => ({ label, tone: "warning", detail: "" });
  assert.equal(canRetrySource(status("处理失败"), source), true);
  assert.equal(canRetrySource(status("未编译"), source), true);
  assert.equal(canRetrySource(status("未通过"), source), true);
  assert.equal(canRetrySource(status("索引失败"), source), false);
  assert.equal(canRetrySource(status("已发布"), source), false);
  assert.equal(canRetrySource(status("处理失败"), { ...source, current_revision_id: null }), false);
});

test("a visible failed source does not need a second error banner", () => {
  const failed = compilation("failed", "OpenAIInvalidRequestError");
  assert.equal(hasVisibleSourceForRun(failed, [source]), true);
  assert.equal(hasVisibleSourceForRun(failed, []), false);
});
test("bidirectional graph validation failures explain the missing relationship without exposing paths", () => {
  const message = importFailureMessage(
    "WikiRepositoryError: entities/agent-preset.md and sources/private-id.md do not have a bidirectional link"
  );
  assert.match(message, /相互关联/);
  assert.ok(!message.includes("private-id"));
  assert.ok(!message.includes("模型配置"));
});
