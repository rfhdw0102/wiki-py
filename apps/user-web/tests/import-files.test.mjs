import assert from "node:assert/strict";
import test from "node:test";
import {
  droppedFiles,
  prepareImportFiles,
  selectedFiles
} from "../src/app/wiki/import-files.ts";

test("folder selection includes nested supported files and explicitly lists skipped files", () => {
  const markdown = new File(["# Hello"], "one.md");
  const html = new File(["<p>Hello</p>"], "two.html");
  const zip = new File(["archive"], "archive.zip");
  const empty = new File([], "empty.txt");
  Object.defineProperty(markdown, "webkitRelativePath", { value: "notes/one.md" });
  Object.defineProperty(html, "webkitRelativePath", { value: "notes/nested/two.html" });
  Object.defineProperty(zip, "webkitRelativePath", { value: "notes/archive.zip" });
  Object.defineProperty(empty, "webkitRelativePath", { value: "notes/empty.txt" });
  const { supported, skipped } = prepareImportFiles(
    selectedFiles([markdown, html, zip, empty])
  );
  assert.deepEqual(supported.map(({ path }) => path), [
    "notes/one.md", "notes/nested/two.html"
  ]);
  assert.deepEqual(skipped, [
    "notes/archive.zip（不支持的格式）",
    "notes/empty.txt（空文件）"
  ]);
});

test("dropped folders read every directory-reader batch, including nested directories", async () => {
  const fileEntry = (path, name) => ({
    isFile: true,
    isDirectory: false,
    fullPath: path,
    file: (resolve) => resolve(new File(["content"], name))
  });
  const child = fileEntry("/notes/nested/child.txt", "child.txt");
  const nested = {
    isFile: false,
    isDirectory: true,
    createReader: () => {
      let done = false;
      return { readEntries: (resolve) => {
        resolve(done ? [] : [child]);
        done = true;
      } };
    }
  };
  const root = {
    isFile: false,
    isDirectory: true,
    createReader: () => {
      let index = 0;
      const batches = [[fileEntry("/notes/root.md", "root.md")], [nested], []];
      return { readEntries: (resolve) => resolve(batches[index++]) };
    }
  };
  const files = await droppedFiles({
    items: [{ kind: "file", webkitGetAsEntry: () => root }],
    files: []
  });
  assert.deepEqual(files.map(({ path }) => path), [
    "notes/root.md", "notes/nested/child.txt"
  ]);
});

test("directory read failures surface instead of silently losing files", async () => {
  await assert.rejects(
    droppedFiles({
      items: [{ kind: "file", webkitGetAsEntry: () => ({
        isFile: false,
        isDirectory: true,
        createReader: () => ({ readEntries: (_resolve, reject) => reject(new Error("read failed")) })
      }) }],
      files: []
    }),
    /read failed/
  );
});

test("partial folder support never silently drops a dragged directory", async () => {
  await assert.rejects(
    droppedFiles({
      items: [
        { kind: "file", webkitGetAsEntry: () => ({ isFile: true }) },
        { kind: "file", webkitGetAsEntry: () => null }
      ],
      files: [new File(["ok"], "ok.txt")]
    }),
    /无法读取拖入的部分文件夹/
  );
});
