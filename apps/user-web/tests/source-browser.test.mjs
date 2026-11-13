import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import ts from "typescript";
import * as management from "../src/app/sources/source-management.ts";
import * as processing from "../src/app/wiki/source-processing.ts";
import * as deletion from "../src/app/sources/source-deletion.ts";
import * as importFiles from "../src/app/wiki/import-files.ts";

const require = createRequire(import.meta.url);
const sourceCode = readFileSync(new URL("../src/app/sources/source-browser.tsx", import.meta.url), "utf8");
const transpiled = ts.transpileModule(sourceCode, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
const settle = () => new Promise((resolve) => setImmediate(resolve));
const source = (id, name = `${id}.txt`) => ({
  id, name, kind: "text", current_revision_id: `revision-${id}`, created_at: "2026-10-10"
});

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function mountSourceBrowser(sources, api = {}, runs = []) {
  const state = [];
  let stateIndex = 0;
  let resource = { sources, runs, drafts: [], features: [{ key: "source_upload", enabled: true }] };
  const module = { exports: {} };
  runInNewContext(transpiled, {
    Error,
    exports: module.exports,
    module,
    require: (name) => {
      if (name === "react") return {
        ...React,
        useState: (initial) => {
          const index = stateIndex++;
          if (!(index in state)) state[index] = typeof initial === "function" ? initial() : initial;
          return [state[index], (value) => {
            state[index] = typeof value === "function" ? value(state[index]) : value;
          }];
        },
        useRef: (initial) => {
          const index = stateIndex++;
          state[index] ??= { current: initial };
          return state[index];
        },
        useEffect: () => {},
        useCallback: (callback) => callback,
        useMemo: (factory) => factory()
      };
      if (name === "@wiki-agent/web-ui") return {
        api,
        ApiError,
        useApiResource: () => ({
          data: resource, loading: false, error: null, reload: () => {},
          setData: (update) => { resource = update(resource); }
        }),
        RequestState: () => null,
        StatusBadge: ({ children }) => React.createElement("span", null, children),
        formatDateTime: (value) => value,
        formatLabel: (value) => value
      };
      if (name === "../wiki/source-processing") return processing;
      if (name === "./source-management") return {
        ...management,
        runSourceBatch: (...args) => management.runSourceBatch(...args, async () => {})
      };
      if (name === "./source-deletion") return deletion;
      if (name === "../wiki/import-files") return importFiles;
      if (name === "./source-preview" ||
        name === "./web-browser-import" || name === "./website-import") return {};
      if (name === "next/link" || name === "react-markdown" || name === "remark-gfm") return {};
      return require(name);
    }
  });
  return {
    render() {
      stateIndex = 0;
      return module.exports.SourceBrowser();
    },
    item(id) {
      const tree = this.render();
      const item = find(tree, (element) => element.type?.name === "SourceItem" && element.props.source.id === id);
      assert.ok(item, `missing source ${id}`);
      return item;
    },
    resource: () => resource
  };
}

function find(tree, predicate) {
  if (!tree || typeof tree !== "object") return null;
  if (Array.isArray(tree)) {
    for (const child of tree) {
      const found = find(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (predicate(tree)) return tree;
  return find(tree.props?.children, predicate);
}

function text(tree) {
  if (tree === null || tree === undefined || typeof tree === "boolean") return "";
  if (typeof tree !== "object") return String(tree);
  if (Array.isArray(tree)) return tree.map(text).join("");
  return text(tree.props?.children);
}

function button(tree, label) {
  const result = find(tree, (element) => element.type === "button" &&
    (element.props["aria-label"] === label || text(element) === label));
  assert.ok(result, `missing button ${label}`);
  return result;
}

test("actual source JSX supports filtered select-all, mixed state and checkbox-safe preview", () => {
  const view = mountSourceBrowser([source("a", "Alpha"), source("b", "Beta"), source("c", "Alpha second")]);
  view.item("b").props.onCheck();
  const search = find(view.render(), (element) => element.props?.["aria-label"] === "搜索原件");
  search.props.onChange({ target: { value: "Alpha" } });
  const selectAll = () => find(view.render(), (element) => element.props?.ref && element.type === "input");
  assert.equal(selectAll().props.checked, false);
  selectAll().props.onChange();
  assert.equal(selectAll().props.checked, true);
  assert.equal(view.item("a").props.checked, true);
  assert.equal(view.item("c").props.checked, true);
  view.item("a").props.onCheck();
  assert.equal(selectAll().props["aria-checked"], "mixed");
  search.props.onChange({ target: { value: "" } });
  assert.equal(view.item("b").props.checked, true);
  const item = view.item("b");
  const article = item.type(item.props);
  article.props.onClick({ target: { closest: () => ({ tagName: "INPUT" }) } });
  assert.equal(view.item("b").props.selected, false);
  article.props.onClick({ target: { closest: () => null } });
  assert.equal(view.item("b").props.selected, true);
});

test("actual bulk-delete JSX confirms names/count and retains only failed selected items", async () => {
  const calls = [];
  const view = mountSourceBrowser([source("a"), source("b"), source("c")], {
    deleteSource: async (id) => {
      calls.push(id);
      if (id === "b") throw new ApiError("Source is still being processed", 409);
    }
  });
  for (const id of ["a", "b", "c"]) view.item(id).props.onCheck();
  button(view.render(), "批量删除").props.onClick();
  const dialog = find(view.render(), (element) => element.props?.["aria-labelledby"] === "bulk-delete-source-title");
  assert.match(text(dialog), /删除 3 份知识来源/);
  assert.match(text(dialog), /a.txt.*b.txt.*c.txt/);
  assert.match(text(dialog), /删除无法撤销/);
  button(dialog, "确认删除 3 份").props.onClick();
  assert.equal(button(view.render(), "批量删除").props.disabled, true);
  await settle();
  assert.deepEqual(calls, ["a", "b", "c"]);
  assert.deepEqual(view.resource().sources.map((item) => item.id), ["b"]);
  assert.equal(view.item("b").props.checked, true);
  const report = find(view.render(), (element) => element.props?.role === "alert");
  assert.match(text(report), /成功 2 份，失败 1 份/);
  assert.match(text(report), /b.txt.*资料仍在处理/);
});

test("bulk compilation queues exactly one shared run and skips published sources", async () => {
  const calls = [];
  const view = mountSourceBrowser([source("a"), source("b"), source("published")], {
    compileSourceBatch: async (ids) => {
      calls.push([...ids]);
      return {
        id: "shared-run", status: "queued", kind: "compile_source",
        input_json: { batch: true, source_revision_id: "revision-a", source_revision_ids: ["revision-a", "revision-b"] }
      };
    },
    retrySource: async () => {
      assert.fail("a batch must not retry individual sources");
    }
  }, [{
    id: "published-run", kind: "compile_source", status: "completed",
    input_json: { source_revision_id: "revision-published" }, result_json: { published_commit: "commit" }
  }]);
  for (const id of ["a", "b", "published"]) view.item(id).props.onCheck();
  button(view.render(), "批量编译（2）").props.onClick();
  await settle();
  assert.deepEqual(calls, [["a", "b"]]);
  assert.match(text(view.render()), /已提交 2 份，跳过 1 份，失败 0 份/);
  assert.match(text(view.render()), /已跳过“published.txt”/);
  assert.equal(view.resource().runs.filter((run) => run.id === "shared-run").length, 1);
  assert.equal(view.item("a").props.status.label, "等待处理");
  assert.equal(view.item("b").props.status.label, "等待处理");
});

test("failed batch admission never retries individual members or reports partial success", async () => {
  let batchCalls = 0;
  const view = mountSourceBrowser([source("a"), source("b"), source("published")], {
    compileSourceBatch: async () => { batchCalls++; throw new Error("queue offline"); },
    retrySource: async () => { assert.fail("no independent retry fallback"); }
  }, [{
    id: "published", kind: "compile_source", status: "completed",
    input_json: { source_revision_id: "revision-published" }, result_json: { published_commit: "commit" }
  }]);
  for (const id of ["a", "b", "published"]) view.item(id).props.onCheck();
  button(view.render(), "批量编译（2）").props.onClick();
  await settle();
  assert.equal(batchCalls, 1);
  assert.match(text(view.render()), /已提交 0 份，跳过 1 份，失败 2 份/);
  assert.match(text(view.render()), /整批未成功进入队列.*未逐份重试/);
  assert.match(text(view.render()), /queue offline/);
  assert.match(text(view.render()), /已跳过“published.txt”/);
  assert.equal(view.item("a").props.checked, true);
  assert.equal(view.item("b").props.checked, true);
  assert.equal(view.resource().runs.length, 1);
});

test("a batch run missing members is not treated as a successful admission", async () => {
  const view = mountSourceBrowser([source("a"), source("b")], {
    compileSourceBatch: async () => ({
      id: "incomplete", status: "queued", kind: "compile_source",
      input_json: { source_revision_id: "revision-a" }
    })
  });
  for (const id of ["a", "b"]) view.item(id).props.onCheck();
  button(view.render(), "批量编译（2）").props.onClick();
  await settle();
  assert.match(text(view.render()), /已提交 0 份，跳过 0 份，失败 2 份/);
  assert.match(text(view.render()), /未确认本批次全部资料进入同一个编译任务/);
  assert.equal(view.resource().runs.length, 0);
});

test("one selected failed batch member uses existing retry and reflects the expanded shared run", async () => {
  const calls = [];
  const view = mountSourceBrowser([source("a"), source("b")], {
    retrySource: async (id) => {
      calls.push(id);
      return {
        id: "retry-batch", status: "queued", kind: "compile_source",
        input_json: { batch: true, source_revision_id: "revision-a", source_revision_ids: ["revision-a", "revision-b"] }
      };
    },
    compileSourceBatch: async () => { assert.fail("single selection uses existing retry"); }
  });
  view.item("b").props.onCheck();
  button(view.render(), "批量编译（1）").props.onClick();
  await settle();
  assert.deepEqual(calls, ["b"]);
  assert.equal(view.item("a").props.status.label, "等待处理");
  assert.equal(view.item("b").props.status.label, "等待处理");
  assert.match(text(view.render()), /已提交 2 份，跳过 0 份，失败 0 份/);
});

function selectImport(view, files, folder = false) {
  button(view.render(), "导入文件或网址").props.onClick();
  const input = find(view.render(), (element) => element.type === "input" && element.props.type === "file" &&
    (folder ? element.props["aria-label"] === "选择文件夹" : Boolean(element.props.accept)));
  assert.ok(input);
  input.props.onChange({ target: { files, value: "selected", hasAttribute: () => folder } });
}

test("multi-file import makes one upload request and reports invalid and skipped files", async () => {
  const calls = [];
  const files = [new File(["one"], "one.md"), new File(["two"], "two.txt"),
    new File(["bad"], "invalid.pdf"), new File(["zip"], "archive.zip"), new File([], "empty.txt")];
  const view = mountSourceBrowser([], {
    uploadSource: async () => { assert.fail("multi-file upload must not loop over uploadSource"); },
    uploadSourceBatch: async (accepted) => {
      calls.push(accepted.map((file) => file.name));
      return {
        compilation_run_id: "shared", compilation_status: "queued", compilation_error: null,
        items: accepted.map((file) => ({
          file_name: file.name,
          revision: file.name === "invalid.pdf" ? null : { compilation_run_id: "shared", compilation_status: "queued" },
          error: file.name === "invalid.pdf" ? "无法解析 PDF" : null
        }))
      };
    }
  });
  selectImport(view, files);
  await settle();
  assert.deepEqual(calls, [["one.md", "two.txt", "invalid.pdf"]]);
  assert.match(text(view.render()), /2 份资料已保存，同一个编译任务已排队/);
  assert.match(text(view.render()), /invalid.pdf：无法解析 PDF/);
  assert.match(text(view.render()), /archive.zip（不支持的格式）/);
  assert.match(text(view.render()), /empty.txt（空文件）/);
});

test("folder and drop gestures use the batch endpoint even for one accepted file", async () => {
  const calls = [];
  const view = mountSourceBrowser([], {
    uploadSourceBatch: async (files) => {
      calls.push(files.map((file) => file.name));
      return {
        compilation_run_id: "shared", compilation_status: "queued", compilation_error: null,
        items: files.map((file) => ({
          file_name: file.name, error: null,
          revision: { compilation_run_id: "shared", compilation_status: "queued" }
        }))
      };
    }
  });
  selectImport(view, [new File(["one"], "folder.md")], true);
  await settle();
  button(view.render(), "导入文件或网址").props.onClick();
  const drop = find(view.render(), (element) => Boolean(element.props?.onDrop));
  await drop.props.onDrop({
    preventDefault() {},
    dataTransfer: { items: [], files: [new File(["two"], "drop.txt")] }
  });
  assert.deepEqual(calls, [["folder.md"], ["drop.txt"]]);
});

test("batch upload queue failure has no success notice and large gestures are never capped or split", async () => {
  const counts = [];
  const view = mountSourceBrowser([], {
    uploadSourceBatch: async (files) => {
      counts.push(files.length);
      return {
        compilation_run_id: "failed", compilation_status: "failed", compilation_error: "Queue submission failed",
        items: files.map((file) => ({
          file_name: file.name, error: null,
          revision: { compilation_run_id: "failed", compilation_status: "failed" }
        }))
      };
    }
  });

  test("failed batch upload requests retain skipped-file errors without claiming admission", async () => {
    const view = mountSourceBrowser([], {
      uploadSourceBatch: async () => { throw new ApiError("queue offline", 503); },
      uploadSource: async () => { assert.fail("a failed batch must not fall back to independent uploads"); }
    });
    selectImport(view, [new File(["one"], "one.md"), new File(["two"], "two.md"), new File([], "empty.txt")]);
    await settle();
    assert.match(text(view.render()), /empty.txt（空文件）/);
    assert.match(text(view.render()), /整次导入未成功提交，未确认已保存或排队：queue offline/);
    assert.doesNotMatch(text(view.render()), /资料已保存，同一个编译任务已排队/);
  });
  selectImport(view, [new File(["one"], "one.md"), new File(["two"], "two.md")]);
  await settle();
  assert.match(text(view.render()), /整批编译未成功提交/);
  assert.doesNotMatch(text(view.render()), /资料已保存，同一个编译任务已排队/);
  const many = Array.from({ length: 101 }, (_, index) => new File(["ok"], `${index}.md`));
  selectImport(view, many);
  await settle();
  assert.deepEqual(counts, [2, 101]);
  assert.match(text(view.render()), /整批编译未成功提交/);
});

test("clicking a filename edits inline, retains failures and updates names without a dialog", async () => {
  let shouldFail = true;
  const calls = [];
  const view = mountSourceBrowser([source("a")], {
    renameSource: async (id, name) => {
      calls.push([id, name]);
      if (shouldFail) throw new Error("rename offline");
      return source(id, name);
    }
  });
  view.item("a").props.onOpen();
  view.item("a").props.onRename();
  assert.equal(find(view.render(), (element) => element.props?.role === "dialog"), null);
  let editor = view.item("a").props.titleEditor;
  find(editor, (element) => element.type === "input").props.onChange({ target: { value: "  新名称  " } });
  editor = view.item("a").props.titleEditor;
  find(editor, (element) => element.type === "input").props.onKeyDown({
    key: "Enter", preventDefault() {}, stopPropagation() {}
  });
  await settle();
  assert.match(text(view.item("a").props.titleEditor), /rename offline/);
  assert.equal(view.resource().sources[0].name, "a.txt");
  shouldFail = false;
  find(view.item("a").props.titleEditor, (element) => element.type === "input").props.onBlur();
  await settle();
  assert.deepEqual(calls, [["a", "新名称"], ["a", "新名称"]]);
  assert.equal(view.resource().sources[0].name, "新名称");
  const preview = find(view.render(), (element) => element.type?.name === "SourcePreview");
  assert.equal(preview.props.source.name, "新名称");
  find(view.render(), (element) => element.props?.["aria-label"] === "搜索原件")
    .props.onChange({ target: { value: "新名称" } });
  assert.ok(view.item("a"));
});

test("actual single-delete dialog still closes immediately before its request settles", async () => {
  let complete;
  const view = mountSourceBrowser([source("a")], {
    deleteSource: () => new Promise((resolve) => { complete = resolve; })
  });
  view.item("a").props.onDelete();
  const dialog = find(view.render(), (element) => element.props?.["aria-labelledby"] === "delete-source-title");
  button(dialog, "确认删除").props.onClick();
  assert.equal(find(view.render(), (element) => element.props?.["aria-labelledby"] === "delete-source-title"), null);
  assert.equal(view.item("a").props.busy, true);
  complete();
  await settle();
  assert.equal(view.resource().sources.length, 0);
});

test("busy bulk compilation blocks overlapping import, rename, deletion and repeated compilation", async () => {
  let complete;
  let requests = 0;
  const view = mountSourceBrowser([source("a")], {
    retrySource: () => {
      requests++;
      return new Promise((resolve) => { complete = resolve; });
    }
  });
  view.item("a").props.onCheck();
  const staleItem = view.item("a");
  const staleImport = find(view.render(), (element) => element.props?.["aria-label"] === "导入文件或网址");
  const compile = button(view.render(), "批量编译（1）");
  compile.props.onClick();
  compile.props.onClick();
  staleItem.props.onRename();
  staleImport.props.onClick();
  assert.equal(requests, 1);
  assert.equal(view.item("a").props.busy, true);
  assert.equal(button(view.render(), "批量删除").props.disabled, true);
  assert.equal(view.item("a").props.titleEditor, null);
  assert.equal(find(view.render(), (element) => element.props?.["aria-labelledby"] === "import-dialog-title"), null);
  complete({ id: "queued", kind: "compile_source", status: "queued", input_json: { source_revision_id: "revision-a" } });
  await settle();
  assert.equal(view.item("a").props.busy, false);
});

test("a non-queued compilation response is a failure, never a successful fallback", async () => {
  const view = mountSourceBrowser([source("a")], {
    retrySource: async () => ({ id: "failed-run", status: "failed" })
  });
  view.item("a").props.onCheck();
  button(view.render(), "批量编译（1）").props.onClick();
  await settle();
  assert.match(text(view.render()), /已提交 0 份，跳过 0 份，失败 1 份/);
  assert.match(text(view.render()), /编译任务未进入队列/);
  assert.equal(view.item("a").props.checked, true);
  assert.equal(view.resource().runs.length, 0);
});

test("Escape cancels filename changes even when blur follows and does not call rename", async () => {
  const calls = [];
  const view = mountSourceBrowser([source("a")], {
    renameSource: async (...args) => { calls.push(args); return source("a"); }
  });
  view.item("a").props.onRename();
  let editor = view.item("a").props.titleEditor;
  find(editor, (element) => element.type === "input")
    .props.onChange({ target: { value: "取消的名称" } });
  editor = view.item("a").props.titleEditor;
  const input = find(editor, (element) => element.type === "input");
  input.props.onKeyDown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  input.props.onBlur();
  await settle();
  assert.deepEqual(calls, []);
  assert.equal(view.item("a").props.titleEditor, null);
  assert.equal(view.resource().sources[0].name, "a.txt");
});

test("preview filename supports the same inline editing and blur-save as the list", async () => {
  const view = mountSourceBrowser([source("a")], {
    renameSource: async (id, name) => source(id, name)
  });
  view.item("a").props.onOpen();
  let preview = find(view.render(), (element) => element.type?.name === "SourcePreview");
  preview.props.onRename(preview.props.source);
  preview = find(view.render(), (element) => element.type?.name === "SourcePreview");
  assert.equal(view.item("a").props.titleEditor, null);
  find(preview.props.titleEditor, (element) => element.type === "input")
    .props.onChange({ target: { value: "预览新名称" } });
  preview = find(view.render(), (element) => element.type?.name === "SourcePreview");
  find(preview.props.titleEditor, (element) => element.type === "input").props.onBlur();
  await settle();
  assert.equal(view.resource().sources[0].name, "预览新名称");
});

test("bulk controls form a compact single row without large primary or danger buttons", () => {
  const view = mountSourceBrowser([source("a")]);
  view.item("a").props.onCheck();
  const toolbar = find(view.render(), (element) => element.props?.className === "source-selection-toolbar");
  const compile = button(toolbar, "批量编译（1）");
  assert.equal(compile.props.className, "source-batch-button");
  assert.equal(text(compile), "编译");
  const remove = button(toolbar, "批量删除");
  assert.equal(remove.props.className, "source-batch-button source-batch-button-delete");
  assert.equal(text(remove), "删除");
  assert.doesNotMatch(text(toolbar), /当前列表.*份|不符合编译条件/);
});
