import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);

function loadComponent(relativePath, modules = {}) {
  const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
  const output = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  const exports = {};
  runInNewContext(output, {
    exports,
    Math,
    Number,
    Intl,
    Date,
    require: (name) => Object.hasOwn(modules, name) ? modules[name] : require(name)
  });
  return exports;
}

const { Pagination } = loadComponent("../../../packages/web-ui/src/primitives.tsx");
const sizes = [10, 20, 50, 100];
const paginationProps = {
  page: 1, pageCount: 86, pageSize: 20, pageSizeOptions: sizes,
  onPageChange: () => {}, onPageSizeChange: () => {}
};

function findElement(root, predicate) {
  if (!React.isValidElement(root)) return undefined;
  if (predicate(root)) return root;
  for (const child of React.Children.toArray(root.props.children)) {
    const found = findElement(child, predicate);
    if (found) return found;
  }
  return undefined;
}

function numberedPages(root) {
  return React.Children.toArray(root.props.children)
    .filter((child) => React.isValidElement(child) && /^第 \d+ 页$/.test(child.props["aria-label"]))
    .map((child) => child.props.children);
}

test("pagination renders numbered pages, ellipsis and the requested size choices", () => {
  const root = Pagination(paginationProps);
  assert.deepEqual(numberedPages(root), [1, 2, 3, 4, 5, 86]);
  const markup = renderToStaticMarkup(root);
  assert.match(markup, /aria-current="page"/);
  assert.match(markup, /pagination-gap/);
  assert.match(markup, /<option value="20" selected="">20 条\/页<\/option>/);
  for (const size of sizes) assert.ok(markup.includes(`${size} 条/页`));
  assert.equal(findElement(root, (el) => el.props["aria-label"] === "上一页").props.disabled, true);
  assert.equal(findElement(root, (el) => el.props["aria-label"] === "下一页").props.disabled, false);
});

test("numbered pagination stays bounded and includes the active, first and last pages", () => {
  for (const pageCount of [1, 2, 7, 8, 86, 1000]) {
    for (const page of new Set([1, Math.ceil(pageCount / 2), pageCount])) {
      const root = Pagination({ ...paginationProps, page, pageCount });
      const pages = numberedPages(root);
      assert.ok(pages.length <= 9);
      assert.equal(new Set(pages).size, pages.length);
      assert.equal(pages[0], 1);
      assert.equal(pages.at(-1), pageCount);
      assert.ok(pages.includes(page));
      assert.equal(
        findElement(root, (el) => el.props["aria-label"] === "下一页").props.disabled,
        page === pageCount
      );
    }
  }
  assert.deepEqual(numberedPages(Pagination({ ...paginationProps, page: 43 })), [
    1, 41, 42, 43, 44, 45, 86
  ]);
});

test("page, previous/next and page-size controls invoke their controlled callbacks", () => {
  const pages = [], pageSizes = [];
  const root = Pagination({
    ...paginationProps, page: 3,
    onPageChange: (value) => pages.push(value),
    onPageSizeChange: (value) => pageSizes.push(value)
  });
  findElement(root, (el) => el.props["aria-label"] === "上一页").props.onClick();
  findElement(root, (el) => el.props["aria-label"] === "下一页").props.onClick();
  findElement(root, (el) => el.props["aria-label"] === "第 86 页").props.onClick();
  findElement(root, (el) => el.type === "select").props.onChange({ target: { value: "100" } });
  assert.deepEqual(pages, [2, 4, 86]);
  assert.deepEqual(pageSizes, [100]);
});

function logPageHarness() {
  const states = [], requests = [], effects = [];
  let cursor = 0;
  let data = { items: [], total: 1720 };
  const module = loadComponent("../src/app/logs/page.tsx", {
    react: {
      ...React,
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
      useState: (initial) => {
        const index = cursor++;
        if (!(index in states)) states[index] = initial;
        return [states[index], (value) => {
          states[index] = typeof value === "function" ? value(states[index]) : value;
        }];
      }
    },
    "next/link": () => null,
    "@wiki-agent/web-ui": {
      DataTable: () => null, EmptyState: () => null, RequestState: () => null,
      StatusBadge: () => null, Pagination,
      api: { listLogs: async (request) => { requests.push(request); return data; } },
      useApiResource: (load) => {
        void load();
        return { data, error: null, loading: false, reload: () => {} };
      }
    },
    "../wiki/source-processing": {},
    "./log-description": {}
  });
  return {
    requests,
    effects,
    setData: (value) => { data = value; },
    render: () => {
      cursor = 0;
      effects.length = 0;
      return module.default();
    }
  };
}

test("logs use server-side offset/limit, default 20, and reset to first page on size/filter changes", () => {
  const harness = logPageHarness();
  let page = harness.render();
  let controls = findElement(page, (el) => el.type === Pagination);
  assert.equal(controls.props.pageSize, 20);
  assert.equal(controls.props.pageCount, 86);
  assert.deepEqual(Array.from(controls.props.pageSizeOptions), sizes);
  assert.equal(harness.requests.at(-1).offset, 0);
  assert.equal(harness.requests.at(-1).limit, 20);

  controls.props.onPageChange(3);
  page = harness.render();
  assert.equal(harness.requests.at(-1).offset, 40);
  for (const size of sizes) {
    controls = findElement(page, (el) => el.type === Pagination);
    controls.props.onPageSizeChange(size);
    page = harness.render();
    assert.equal(harness.requests.at(-1).offset, 0);
    assert.equal(harness.requests.at(-1).limit, size);
    controls = findElement(page, (el) => el.type === Pagination);
    assert.equal(controls.props.pageCount, Math.ceil(1720 / size));
    controls.props.onPageChange(2);
    page = harness.render();
    assert.equal(harness.requests.at(-1).offset, size);
  }

  const category = findElement(page, (el) => el.type === "select" && el.props.value === "all");
  category.props.onChange({ target: { value: "error" } });
  harness.render();
  assert.equal(harness.requests.at(-1).category, "error");
  assert.equal(harness.requests.at(-1).offset, 0);
  assert.equal(harness.requests.at(-1).limit, 100);
});

test("logs clamp a vanished last page after refresh and keep the size selector with empty results", () => {
  const harness = logPageHarness();
  let root = harness.render();
  findElement(root, (el) => el.type === Pagination).props.onPageChange(86);
  harness.render();
  assert.equal(harness.requests.at(-1).offset, 1700);
  harness.setData({ items: [], total: 25 });
  harness.render();
  harness.effects.at(-1)();
  root = harness.render();
  assert.equal(harness.requests.at(-1).offset, 20);
  assert.equal(findElement(root, (el) => el.type === Pagination).props.page, 2);

  harness.setData({ items: [], total: 0 });
  harness.render();
  harness.effects.at(-1)();
  root = harness.render();
  const controls = findElement(root, (el) => el.type === Pagination);
  assert.equal(controls.props.pageCount, 1);
  assert.equal(controls.props.page, 1);
  assert.equal(controls.props.disabled, false);
  assert.equal(harness.requests.at(-1).offset, 0);
});
