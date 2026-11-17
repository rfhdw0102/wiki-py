import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import ts from "typescript";
import {
  groupConflicts, openDrafts, reviewExplanationError, reviewFailureMessage, reviewReasons
} from "../src/app/conflicts/review.ts";
import * as review from "../src/app/conflicts/review.ts";
import * as processing from "../src/app/wiki/source-processing.ts";

test("publishing accepts an optional explanation while rejection still needs a reason", () => {
  for (const explanation of ["", "   ", "好", "更新来源摘要"]) {
    assert.equal(reviewExplanationError("publish", explanation), null);
  }
  for (const explanation of ["", "   ", "否", " 不要 "]) {
    assert.match(reviewExplanationError("reject", explanation), /至少 3 个字符/);
  }
  assert.equal(reviewExplanationError("reject", " 来源不可靠 "), null);
});

test("rejection explains protected references and partial file cleanup failures", () => {
  assert.match(reviewFailureMessage(
    "Source is referenced by Wiki pages; remove the references first"
  ), /已被发布.*草稿和原件均保留/);
  assert.match(reviewFailureMessage(
    "Source is referenced by a draft; remove the draft first"
  ), /其他草稿.*草稿和原件均保留/);
  assert.match(reviewFailureMessage("Source is still being processed"), /等待处理结束/);
  assert.match(reviewFailureMessage("Source deleted, but object cleanup failed"), /文件清理失败/);
  assert.equal(reviewFailureMessage("服务暂时不可用"), "服务暂时不可用");
});
test("only drafts awaiting a decision appear in the review list", () => {
  const drafts = [
    { id: "open", status: "open" },
    { id: "published", status: "published" },
    { id: "rejected", status: "rejected" }
  ];
  assert.deepEqual(openDrafts(drafts).map((draft) => draft.id), ["open"]);
});

test("conflicts and unrelated review risks are explained independently", () => {
  const draft = { id: "d", files: { "sources/source.md": "content" } };
  const runs = [{
    kind: "compile_source",
    result_json: {
      draft_id: "d",
      risk_violations: ["too_many_changed_pages", "unresolved_conflicts"]
    }
  }];
  assert.deepEqual(reviewReasons(draft, runs), [
    "改动页面过多", "存在待处理冲突"
  ]);
  assert.deepEqual(reviewReasons(
    { ...draft, files: { "pending/conflict.md": "content" } }, []
  ), ["存在待处理冲突"]);
  assert.deepEqual(reviewReasons(draft, []), []);
});

const conflict = (path, batch_id = null, sources = ["sources/one.md"], updated = "2026-10-10T00:00:00Z") => ({
  path, title: `Conflict ${path}`, batch_id, batch_title: batch_id ? "同一批资料" : null,
  status: "pending", sources, updated
});

test("published conflicts group by batch, union source paths and retain independent legacy pages", () => {
  const groups = groupConflicts([
    conflict("pending/first.md", "batch-one", ["sources/one.md", "sources/two.md"]),
    conflict("pending/legacy-one.md"),
    conflict("pending/second.md", "batch-one", ["sources/two.md", "sources/three.md"], "2026-10-10T01:00:00Z"),
    conflict("pending/legacy-two.md"),
    { ...conflict("pending/other.md", "batch-two"), batch_title: null }
  ]);
  assert.equal(groups.length, 4);
  const batch = groups.find((group) => group.id === "batch:batch-one");
  assert.equal(batch.title, "同一批资料");
  assert.equal(batch.conflicts.length, 2);
  assert.deepEqual(batch.sources, ["sources/one.md", "sources/two.md", "sources/three.md"]);
  assert.equal(batch.updated, "2026-10-10T01:00:00Z");
  assert.equal(groups[0], batch);
  assert.equal(groups.find((group) => group.id === "batch:batch-two").title, "批量导入的待处理冲突");
  assert.equal(groups.filter((group) => group.id.startsWith("page:")).length, 2);
});

test("newest batch update is based on timestamps, not timezone string ordering", () => {
  const groups = groupConflicts([
    conflict("pending/first.md", "batch", [], "2026-10-10T01:00:00+08:00"),
    conflict("pending/second.md", "batch", [], "2026-10-09T23:00:00Z")
  ]);
  assert.equal(groups[0].updated, "2026-10-09T23:00:00Z");
});

const require = createRequire(import.meta.url);
const pageCode = ts.transpileModule(readFileSync(
  new URL("../src/app/conflicts/page.tsx", import.meta.url), "utf8"
), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const settle = () => new Promise((resolve) => setImmediate(resolve));

function mountReview(resource, api = {}) {
  const hooks = [];
  let cursor = 0;
  const exports = {};
  runInNewContext(pageCode, {
    exports, Error,
    require: (name) => {
      if (name === "react") return {
        ...React,
        useState: (initial) => {
          const index = cursor++;
          if (!(index in hooks)) hooks[index] = initial;
          return [hooks[index], (value) => {
            hooks[index] = typeof value === "function" ? value(hooks[index]) : value;
          }];
        },
        useRef: (initial) => {
          const index = cursor++;
          hooks[index] ??= { current: initial };
          return hooks[index];
        },
        useEffect: () => {},
        useCallback: (callback) => callback
      };
      if (name === "@wiki-agent/web-ui") return {
        api, ApiError: class extends Error {},
        formatDateTime: (value) => value, formatLabel: (value) => value,
        Section: () => null, EmptyState: () => null, RequestState: () => null, StatusBadge: () => null,
        useApiResource: () => ({
          data: resource, error: null, loading: false, reload: () => {},
          setData: (update) => { resource = update(resource); }
        })
      };
      if (name === "./review") return review;
      if (name === "../wiki/source-processing") return processing;
      if (name === "next/link") return { default: () => null };
      return require(name);
    }
  });
  return {
    render: () => { cursor = 0; return exports.default(); },
    resource: () => resource
  };
}

function findAll(tree, predicate) {
  if (Array.isArray(tree)) return tree.flatMap((child) => findAll(child, predicate));
  if (!tree || typeof tree !== "object") return [];
  return [...(predicate(tree) ? [tree] : []), ...findAll(tree.props?.children, predicate)];
}

function text(tree) {
  if (tree === null || tree === undefined || typeof tree === "boolean") return "";
  if (Array.isArray(tree)) return tree.map(text).join("");
  return typeof tree === "object" ? text(tree.props?.children) : String(tree);
}

function button(tree, label) {
  const found = findAll(tree, (element) => element.type === "button" && text(element) === label)[0];
  assert.ok(found, `missing button ${label}`);
  return found;
}

const draft = {
  id: "batch-draft", title: "批量资料草稿", status: "open", created_at: "2026-10-10",
  files: { "sources/first.md": "first source", "sources/second.md": "second source", "pending/conflict.md": "conflict content" }
};
const batchRun = {
  id: "shared-run", kind: "compile_source", status: "awaiting_review",
  input_json: { batch: true, source_revision_id: "first", source_revision_ids: ["first", "second"] },
  result_json: { draft_id: draft.id }
};

test("batch review renders one draft, all change previews and grouped published conflict links", () => {
  const view = mountReview({
    drafts: [draft], runs: [batchRun],
    conflicts: [conflict("pending/first.md", "batch"), conflict("pending/second.md", "batch")]
  });
  const tree = view.render();
  assert.equal(findAll(tree, (element) => element.type === "article" && element.props.className === "review-card").length, 1);
  assert.match(text(tree), /2 份来源（整批统一审核）/);
  assert.equal(findAll(tree, (element) => element.props?.className === "review-card conflict-group").length, 1);
  assert.match(text(tree), /2 个冲突页面 · 1 个相关来源/);
  const paths = findAll(tree, (element) => element.props?.href).map((element) => element.props.href);
  assert.ok(paths.includes("/wiki?path=pending%2Ffirst.md"));
  assert.ok(paths.includes("/wiki?path=pending%2Fsecond.md"));
  button(tree, "查看并审核").props.onClick();
  const dialog = findAll(view.render(), (element) => element.props?.role === "dialog")[0];
  assert.match(text(dialog), /整批只能统一发布或拒绝/);
  const previews = findAll(dialog, (element) => element.type === "details");
  assert.equal(previews.length, 3);
  assert.match(text(dialog), /first source.*second source.*conflict content/);
});

test("publishing a batch submits one draft decision even if its submit handler is called twice", async () => {
  const calls = [];
  let finish;
  const view = mountReview({ drafts: [draft], runs: [batchRun], conflicts: [] }, {
    publishDraft: (id, explanation) => {
      calls.push([id, explanation]);
      return new Promise((resolve) => { finish = resolve; });
    }
  });
  button(view.render(), "查看并审核").props.onClick();
  const form = findAll(view.render(), (element) => element.type === "form")[0];
  form.props.onSubmit({ preventDefault() {} });
  form.props.onSubmit({ preventDefault() {} });
  assert.deepEqual(calls, [["batch-draft", ""]]);
  finish({ ...draft, status: "published" });
  await settle();
  assert.equal(findAll(view.render(), (element) => element.type === "article").length, 0);
  assert.equal(view.resource().drafts[0].status, "published");
});

test("rejecting a batch warns about ALL originals and deletes it with one validated decision", async () => {
  const calls = [];
  let finish;
  const unrelated = { ...draft, id: "unrelated", status: "published" };
  const view = mountReview({ drafts: [draft, unrelated], runs: [batchRun], conflicts: [] }, {
    rejectDraft: (id, reason) => {
      calls.push([id, reason]);
      return new Promise((resolve) => { finish = resolve; });
    }
  });
  button(view.render(), "查看并审核").props.onClick();
  button(view.render(), "拒绝发布").props.onClick();
  assert.match(text(view.render()), /本批次全部 2 份导入原件/);
  assert.match(text(view.render()), /无法只拒绝某一份/);
  findAll(view.render(), (element) => element.type === "form")[0].props.onSubmit({ preventDefault() {} });
  assert.equal(calls.length, 0);
  findAll(view.render(), (element) => element.type === "textarea")[0].props.onChange({ target: { value: " 内容不可靠 " } });
  const form = findAll(view.render(), (element) => element.type === "form")[0];
  form.props.onSubmit({ preventDefault() {} });
  form.props.onSubmit({ preventDefault() {} });
  assert.deepEqual(calls, [["batch-draft", "内容不可靠"]]);
  finish();
  await settle();
  assert.deepEqual(view.resource().drafts.map((item) => item.id), ["unrelated"]);
  assert.equal(findAll(view.render(), (element) => element.props?.role === "dialog").length, 0);
});
