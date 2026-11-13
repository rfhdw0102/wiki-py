import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const tick = () => new Promise((resolve) => setImmediate(resolve));
const user = { display_name: "测试用户", email: "test@example.invalid", avatar_url: null };
const wiki = { id: "existing-wiki", name: "自定义旧库名", description: "保留的简介" };
class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function loadComponent(path, modules) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  const code = ts.transpileModule(source, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      esModuleInterop: true
    }
  }).outputText;
  const exports = {};
  const require = createRequire(import.meta.url);
  runInNewContext(code, {
    exports,
    Error,
    require: (name) => Object.hasOwn(modules, name) ? modules[name] : require(name)
  });
  return exports;
}

async function shellHarness(overrides = {}) {
  const states = [], effects = [], activeIds = [], redirects = [];
  let cursor = 0;
  const api = {
    me: async () => user,
    initializeWiki: async () => wiki,
    listWikis: () => { assert.fail("Initialization must not use the old library picker"); },
    deleteWiki: () => { assert.fail("Entering the app must never delete existing libraries"); },
    ...overrides
  };
  const { AppShell } = await loadComponent("../../../packages/web-ui/src/app-shell.tsx", {
    react: {
      ...React,
      useState: (initial) => {
        const index = cursor++;
        if (!(index in states)) states[index] = initial;
        return [states[index], (next) => {
          states[index] = typeof next === "function" ? next(states[index]) : next;
        }];
      },
      useCallback: (callback) => callback,
      useEffect: (effect) => effects.push(effect),
      useRef: () => ({ current: null })
    },
    "next/navigation": {
      usePathname: () => "/sources",
      useRouter: () => ({ replace: (path) => redirects.push(path) })
    },
    "next/link": ({ children, ...props }) => React.createElement("a", props, children),
    "lucide-react": new Proxy({}, { get: () => () => null }),
    "./theme": { ThemeToggle: () => null },
    "./api": { api, ApiError, setActiveWikiId: (id) => activeIds.push(id) }
  });
  function render() {
    cursor = 0;
    effects.length = 0;
    return AppShell({
      productName: "智库 Wiki",
      navItems: [{ href: "/sources", label: "文件导入" }],
      children: React.createElement("span", null, "受保护的知识页面")
    });
  }
  return { render, effects, activeIds, redirects };
}

test("direct entry initializes before rendering children and always uses the fixed library label", async () => {
  let resolve;
  const initialization = new Promise((done) => { resolve = done; });
  let calls = 0;
  const shell = await shellHarness({
    initializeWiki: () => { calls++; return initialization; }
  });
  assert.match(renderToStaticMarkup(shell.render()), /正在准备个人知识库/);
  shell.effects[0]();
  await tick();
  assert.equal(calls, 1);
  assert.deepEqual(shell.activeIds, []);
  assert.doesNotMatch(renderToStaticMarkup(shell.render()), /受保护的知识页面/);

  resolve(wiki);
  await tick();
  const markup = renderToStaticMarkup(shell.render());
  assert.deepEqual(shell.activeIds, [wiki.id]);
  assert.match(markup, /title="个人知识库"/);
  assert.match(markup, /<strong>个人知识库<\/strong>/);
  assert.match(markup, /受保护的知识页面/);
  assert.doesNotMatch(markup, /自定义旧库名|保留的简介|Wiki 名称|创建你的 Wiki/);
  assert.deepEqual(shell.redirects, []);
});

test("initialization failures are visible and can be retried without showing naming forms", async () => {
  let calls = 0;
  const shell = await shellHarness({
    initializeWiki: async () => {
      if (++calls === 1) throw new Error("初始化失败");
      return wiki;
    }
  });
  shell.render();
  shell.effects[0]();
  await tick();
  const failed = shell.render();
  const markup = renderToStaticMarkup(failed);
  assert.match(markup, /role="alert"/);
  assert.match(markup, /初始化失败/);
  assert.doesNotMatch(markup, /受保护的知识页面|<form|<input/);
  assert.deepEqual(shell.activeIds, []);

  failed.props.children[1].props.onClick();
  await tick();
  assert.deepEqual(shell.activeIds, [wiki.id]);
  assert.match(renderToStaticMarkup(shell.render()), /受保护的知识页面/);
});

test("unauthenticated entry still redirects to login without initializing a library", async () => {
  let calls = 0;
  const shell = await shellHarness({
    me: async () => { throw new ApiError("Not authenticated", 401); },
    initializeWiki: async () => { calls++; return wiki; }
  });
  shell.render();
  shell.effects[0]();
  await tick();
  assert.equal(calls, 0);
  assert.deepEqual(shell.activeIds, []);
  assert.deepEqual(shell.redirects, ["/login?next=%2Fsources"]);
});

test("home enters the Wiki directly and never renders a name or description form", async () => {
  const effects = [], redirects = [];
  const { default: HomePage } = await loadComponent("../src/app/page.tsx", {
    react: { ...React, useEffect: (effect) => effects.push(effect) },
    "next/navigation": { useRouter: () => ({ replace: (path) => redirects.push(path) }) },
    "@wiki-agent/web-ui": {
      RequestState: ({ loading }) => React.createElement("div", { role: "status" }, loading ? "正在加载…" : "")
    }
  });
  const markup = renderToStaticMarkup(HomePage());
  effects[0]();
  assert.deepEqual(redirects, ["/wiki"]);
  assert.match(markup, /role="status"/);
  assert.doesNotMatch(markup, /<form|<input|<textarea|Wiki 名称|简介/);
});
