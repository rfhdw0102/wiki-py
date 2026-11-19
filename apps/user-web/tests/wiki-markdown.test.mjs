import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import ts from "typescript";

test("Wiki Markdown renders readable formatting without executing raw HTML", () => {
  const html = renderToStaticMarkup(
    React.createElement(Markdown, { remarkPlugins: [remarkGfm] },
      "# Title\n\n**Important** detail and `code`.\n\n| Item | Value |\n| --- | --- |\n| A | B |\n\n<script>alert('unsafe')</script>"
    )
  );
  assert.match(html, /<h1>Title<\/h1>/);
  assert.match(html, /<strong>Important<\/strong>/);
  assert.match(html, /<code>code<\/code>/);
  assert.match(html, /<table>/);
  assert.doesNotMatch(html, /<script>/);
});

const require = createRequire(import.meta.url);
const answerModule = {};
runInNewContext(ts.transpileModule(
  readFileSync(new URL("../src/app/search/answer-message.tsx", import.meta.url), "utf8"),
  { compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022
  } }
).outputText, {
  exports: answerModule,
  require: (id) => ({
    "react-markdown": { default: Markdown },
    "remark-gfm": { default: remarkGfm },
    "@wiki-agent/web-ui": { StatusBadge: ({ children }) => children }
  })[id] ?? require(id)
});

function renderAnswer(fields) {
  return renderToStaticMarkup(React.createElement(answerModule.AnswerMessage, {
    answer: { citations: [], evidence: [], degradations: [], layer: "wiki", ...fields },
    copied: false,
    onCopy: () => {}
  }));
}

test("answer Markdown links only cited evidence to its Wiki page or exact original span", () => {
  const html = renderAnswer({
    answer: "## 参数\n\n- **电压** [^entities/device.md]\n- 原件 [^raw:span-1]\n\n"
      + "`[^raw:span-1]` [^entities/other.md]\n\n"
      + "[外部链接](https://example.test/) ![图片](https://example.test/photo.png) <img>",
    citations: ["raw:span-1", "entities/device.md", "raw:span-1"],
    evidence: [
      { document_id: "entities/device.md", metadata: { title: "设备" }, channels: [] },
      { document_id: "entities/other.md", metadata: {}, channels: [] },
      { document_id: "raw:span-1", metadata: { source_revision_id: "rev+1", span_id: "span-1" }, channels: [] }
    ]
  });
  assert.match(html, /<h2>参数<\/h2>/);
  assert.match(html, /<strong>电压<\/strong>/);
  assert.match(html, /<a href="\/wiki\?path=entities%2Fdevice\.md" class="answer-citation">\[2\]<\/a>/);
  assert.match(html, /<a href="\/sources\?revision=rev%2B1&amp;span=span-1" class="answer-citation">\[1\]<\/a>/);
  assert.match(html, /<code>\[\^raw:span-1\]<\/code> \[\^entities\/other\.md\]/);
  assert.equal((html.match(/class="evidence-card"/g) ?? []).length, 2);
  assert.doesNotMatch(html, /example\.test|<img/);
});

test("saved answers without inline markers get linked cards without inventing missing targets", () => {
  const html = renderAnswer({
    answer: "旧回答",
    citations: ["concepts/procedure.md", "raw:missing"],
    evidence: [
      { document_id: "concepts/procedure.md", metadata: { title: "流程" }, channels: [] },
      { document_id: "raw:missing", metadata: { heading: "无定位的片段" }, channels: [] }
    ]
  });
  assert.match(html, /<a class="evidence-card" href="\/wiki\?path=concepts%2Fprocedure\.md">/);
  assert.match(html, /<article class="evidence-card">.*无定位的片段/);
  assert.doesNotMatch(html, /answer-citation|\/sources\?/);
});

test("insufficient evidence is actionable in Chinese and not confused with clarification", () => {
  const legacy = renderAnswer({
    answer: "No supporting evidence was found in the Wiki or enabled raw sources.",
    insufficient_evidence: true,
    uncertainty: "The knowledge base does not currently cover this question."
  });
  assert.match(legacy, /知识库中没有找到能支持这个问题的资料/);
  assert.match(legacy, /补充具体名称.*导入相关资料/);
  assert.match(legacy, /证据不足/);
  assert.doesNotMatch(legacy, /supporting evidence|knowledge base/);

  const clarification = renderAnswer({
    answer: "你问的是哪个型号？", clarification_question: "你问的是哪个型号？",
    insufficient_evidence: true, uncertainty: "The reference needs clarification."
  });
  assert.match(clarification, /你问的是哪个型号？/);
  assert.match(clarification, /需要澄清/);
  assert.doesNotMatch(clarification, /证据不足|导入相关资料|The reference/);
});
