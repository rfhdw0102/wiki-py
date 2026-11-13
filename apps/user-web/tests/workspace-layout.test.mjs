import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const navItems = ["/wiki", "/sources", "/conflicts", "/search", "/schema", "/logs"]
  .map((href) => ({ href, label: href }));
const shellSource = readFileSync(
  new URL("../../../packages/web-ui/src/app-shell.tsx", import.meta.url), "utf8"
);
const css = readFileSync(
  new URL("../../../packages/web-ui/src/styles.css", import.meta.url), "utf8"
);
const require = createRequire(import.meta.url);
const shellRequire = createRequire(
  new URL("../../../packages/web-ui/src/app-shell.tsx", import.meta.url)
);

function renderShell(pathname) {
  let stateIndex = 0;
  const module = { exports: {} };
  const transpiled = ts.transpileModule(shellSource, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText;
  runInNewContext(transpiled, {
    exports: module.exports,
    module,
    require: (name) => {
      if (name === "react") return {
        ...React,
        useState: (initial) => [
          stateIndex++ === 0
            ? { display_name: "Test User", email: "test@example.invalid" }
            : typeof initial === "boolean" ? false : initial,
          () => {}
        ],
        useEffect: () => {},
        useCallback: (callback) => callback,
        useRef: () => ({ current: null })
      };
      if (name === "next/navigation") return {
        usePathname: () => pathname, useRouter: () => ({})
      };
      if (name === "next/link") return {
        default: ({ children, ...props }) => React.createElement("a", props, children)
      };
      if (name === "./api") return {};
      if (name === "./theme") return { ThemeToggle: () => null };
      if (name === "lucide-react") return shellRequire(name);
      return require(name);
    }
  });
  return renderToStaticMarkup(React.createElement(module.exports.AppShell, {
    navItems, productName: "Wiki", children: React.createElement("section", null, "Content")
  }));
}

test("all six workspace tabs opt into full-area layout", () => {
  for (const { href } of navItems) {
    const markup = renderShell(href);
    assert.match(markup, /class="shell-content shell-workspace"/);
    assert.match(markup, /class="main-content workspace-content"/);
    assert.ok(markup.includes(`data-page="${href}"`));
  }
});

test("account, onboarding and authentication screens retain their existing layout", () => {
  for (const path of ["/", "/models", "/profile", "/login", "/forgot-password"]) {
    const markup = renderShell(path);
    assert.doesNotMatch(markup, /class="[^"]*(?:shell-workspace|workspace-content)/);
  }
});

test("workspace sizing removes outer gutters and uses remaining height instead of viewport offsets", () => {
  const workspace = css.match(/\.workspace-content \{([^}]+)\}/)[1];
  assert.match(workspace, /padding:\s*0;/);
  assert.match(workspace, /margin:\s*0;/);
  assert.match(workspace, /max-width:\s*none;/);
  assert.match(workspace, /flex:\s*1;/);
  assert.match(workspace, /min-height:\s*0;/);
  assert.match(workspace, /overflow:\s*auto;/);
  assert.match(css, /\.shell-workspace \{[^}]*height:\s*100dvh;/);
  assert.match(css, /\.workspace-content \.chat-workspace \{[^}]*height:\s*auto;/);
  assert.match(css, /\.workspace-content \.wiki-graph-layout \{[^}]*flex:\s*1;/);
  assert.match(css, /\.workspace-content \.wiki-graph:not\(:fullscreen\) \{[^}]*height:\s*100%;/);
  assert.doesNotMatch(css, /@media[^{]*(?:max-width|min-width|pointer|hover)/);
  assert.doesNotMatch(css, /safe-area-inset|touch-action/);
  assert.match(css, /\.workspace-content\[data-page="\/schema"\] > \.panel \{[^}]*flex:\s*1;/);
});

test("only requested outer headers disappear while editors and conversation title remain", () => {
  const wiki = readFileSync(new URL("../src/app/wiki/page.tsx", import.meta.url), "utf8");
  const search = readFileSync(new URL("../src/app/search/page.tsx", import.meta.url), "utf8");
  const schema = readFileSync(new URL("../src/app/schema/page.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(wiki, /<strong>Wiki 内容<\/strong>/);
  assert.doesNotMatch(search, /chat-workspace-head|chat-workspace-title|知识库助手|基于当前 Wiki 与可追溯原始资料回答/);
  assert.match(search, /className="chat-conversation-head"/);
  assert.match(search, /aria-label="对话名称"/);
  assert.doesNotMatch(schema, /<Section|知识编译规则/);
  assert.match(schema, /<section className="panel schema-editor">/);
  assert.match(schema, /<form className="form-grid"/);
  assert.match(schema, /<textarea defaultValue=\{data.content\} name="content" required/);
  const conversationHead = css.match(/\.chat-conversation-head \{([^}]+)\}/)[1];
  assert.doesNotMatch(conversationHead, /border-bottom/);
  assert.match(css, /\.wiki-sources-head,\s*\.wiki-main-head \{[^}]*border-bottom:\s*1px solid var\(--line\)/);
});
