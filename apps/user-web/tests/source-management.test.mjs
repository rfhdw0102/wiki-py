import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  runSourceBatch,
  selectedSources,
  sourceCompilationPlan,
  sourceSelectionState,
  toggleVisibleSources,
  validateSourceName
} from "../src/app/sources/source-management.ts";
import { canRetrySource, sourceIsIdleForCompilation, sourceProcessingStatus } from "../src/app/wiki/source-processing.ts";

const source = (id) => ({
  id, name: `${id}.txt`, kind: "text", current_revision_id: `revision-${id}`
});
const run = (id, status, result_json = null, kind = "compile_source") => ({
  id: `run-${id}-${status}`, kind, status, result_json,
  input_json: { source_revision_id: `revision-${id}` }
});

test("select-all applies only to the filtered list and preserves hidden selections", () => {
  const visible = [source("a"), source("b")];
  const hidden = source("hidden");
  const original = new Set(["a", "hidden"]);
  assert.deepEqual(sourceSelectionState(visible, original), { count: 1, all: false, mixed: true });
  const all = toggleVisibleSources(visible, original);
  assert.deepEqual(all, new Set(["a", "b", "hidden"]));
  assert.deepEqual(original, new Set(["a", "hidden"]));
  assert.deepEqual(sourceSelectionState(visible, all), { count: 2, all: true, mixed: false });
  assert.deepEqual(toggleVisibleSources(visible, all), new Set(["hidden"]));
  assert.deepEqual(toggleVisibleSources([], all), all);
  assert.deepEqual(sourceSelectionState([], all), { count: 0, all: false, mixed: false });
  assert.deepEqual(selectedSources([...visible, hidden], new Set(["b", "hidden", "deleted"])), [
    visible[1], hidden
  ]);
});

test("bulk compilation includes only uncompiled, failed and rejected idle sources", () => {
  const sources = ["idle", "failed", "rejected", "published", "running", "review", "approved", "index", "parse", "old-published"]
    .map(source);
  sources.push({ ...source("no-revision"), current_revision_id: null });
  const runs = [
    run("failed", "failed"),
    run("rejected", "awaiting_review", { draft_id: "rejected-draft" }),
    run("published", "completed", { published_commit: "commit" }),
    run("running", "queued"),
    run("review", "awaiting_review", { draft_id: "open-draft" }),
    run("approved", "awaiting_review", { draft_id: "approved-draft" }),
    run("index", "published_index_failed"),
    run("parse", "running", null, "parse_source"),
    run("old-published", "failed"),
    run("old-published", "completed", { published_commit: "commit" })
  ];
  const drafts = [
    { id: "rejected-draft", status: "rejected" },
    { id: "open-draft", status: "open" },
    { id: "approved-draft", status: "published" }
  ];
  const plan = sourceCompilationPlan(sources, (item) =>
    canRetrySource(sourceProcessingStatus(item, runs, drafts), item) &&
    sourceIsIdleForCompilation(item, runs, drafts)
  );
  assert.deepEqual(plan.ready.map((item) => item.id), ["idle", "failed", "rejected"]);
  assert.equal(plan.skipped.length, 8);
  assert.equal(sourceIsIdleForCompilation(source("review"), [
    run("review", "awaiting_review", { draft_id: "missing" })
  ], []), false);
});

test("batch deletion is sequential, paced and reports partial failures without losing failed selections", async () => {
  const targets = ["a", "b", "c"].map(source);
  let selected = new Set(["a", "b", "c", "hidden"]);
  let active = 0;
  let peak = 0;
  let pauses = 0;
  const progress = [];
  const result = await runSourceBatch(targets, async (item) => {
    active++;
    peak = Math.max(peak, active);
    await Promise.resolve();
    active--;
    if (item.id === "b") throw new Error("Source is still being processed");
    selected.delete(item.id);
  }, (cause) => cause.message, (item, index) => progress.push([item.id, index]),
  async () => { pauses++; });
  assert.equal(peak, 1);
  assert.equal(pauses, 2);
  assert.deepEqual(progress, [["a", 0], ["b", 1], ["c", 2]]);
  assert.deepEqual(result.succeeded.map((item) => item.id), ["a", "c"]);
  assert.deepEqual(result.failed, [{ source: targets[1], message: "Source is still being processed" }]);
  assert.deepEqual(selected, new Set(["b", "hidden"]));
});

test("sequential deletion keeps per-source server failures explicit", async () => {
  const items = ["idle", "failed", "published"].map(source);
  const plan = sourceCompilationPlan(items, (item) => item.id !== "published");
  const submitted = [];
  const result = await runSourceBatch(plan.ready, async (item) => {
    submitted.push(item.id);
    if (item.id === "failed") throw new Error("Queue submission failed");
  }, (cause) => cause.message, () => {}, async () => {});
  assert.deepEqual(submitted, ["idle", "failed"]);
  assert.deepEqual(plan.skipped.map((item) => item.id), ["published"]);
  assert.equal(result.succeeded.length, 1);
  assert.deepEqual(result.failed, [{ source: items[1], message: "Queue submission failed" }]);
  const allFailed = await runSourceBatch(items, async () => { throw new Error("offline"); },
    (cause) => cause.message, () => {}, async () => {});
  assert.equal(allFailed.succeeded.length, 0);
  assert.equal(allFailed.failed.length, 3);
});

test("rename validation trims display names, allows websites and enforces Unicode length", () => {
  assert.equal(validateSourceName("  Website title without extension  "), "Website title without extension");
  assert.equal(validateSourceName("😀".repeat(512)), "😀".repeat(512));
  for (const value of ["", "  ", "a".repeat(513), "a/b", "a\\b", "\nname", "name\r", "a\vb", "a\fb", "a\x1cb", "a\x1db", "a\x1eb", "a\u0085b", "a\u2028b", "a\u2029b"]) {
    assert.throws(() => validateSourceName(value));
  }
});

test("source UI wires mixed selection, checkbox-safe preview, explicit rename save and immediate single-delete close", () => {
  const browser = readFileSync(new URL("../src/app/sources/source-browser.tsx", import.meta.url), "utf8");
  assert.match(browser, /全选当前列表/);
  assert.match(browser, /indeterminate = visibleSelection\.mixed/);
  assert.match(browser, /closest\("button, a, input, label"\)/);
  assert.match(browser, /api\.renameSource\(renameTarget\.id, name\)/);
  assert.match(browser, /renameError \? <span className="source-name-error" role="alert"/);
  assert.match(browser, /onBlur=\{\(\) => void renameSource\(\)\}/);
  assert.doesNotMatch(browser, /rename-source-title/);
  assert.match(browser, /保存/);
  assert.match(browser, /disabled=\{busy \|\| !compilationPlan\.ready\.length\}/);
  const singleDelete = browser.slice(browser.indexOf("async function deleteSource()"), browser.indexOf("function importWebsite"));
  assert.ok(singleDelete.indexOf("setDeleteTarget(null)") < singleDelete.indexOf("await api.deleteSource"));
  assert.match(browser, /删除无法撤销/);
});
