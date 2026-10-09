import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import { sourceMarkdown } from "../src/app/sources/source-preview.ts";

function span(ordinal, heading, content) {
  return {
    id: `revision-1:${ordinal}`,
    source_revision_id: "revision-1",
    ordinal,
    heading,
    content,
    locator: {}
  };
}

test("source preview renders every original heading once at its original level", () => {
  const markdown = sourceMarkdown({
    text: null,
    spans: [
      span(0, "Harness", "# Harness"),
      span(1, "1. Position", "## 1. Position"),
      span(2, "1.1 Summary", "### 1.1 Summary\n\nDetails.")
    ]
  });

  assert.equal(markdown, "# Harness\n\n## 1. Position\n\n### 1.1 Summary\n\nDetails.");
  const html = renderToStaticMarkup(React.createElement(Markdown, {}, markdown));
  assert.deepEqual(
    html.match(/<h[1-6]>.*?<\/h[1-6]>/g),
    ["<h1>Harness</h1>", "<h2>1. Position</h2>", "<h3>1.1 Summary</h3>"]
  );
});

test("continuation chunks do not turn repeated heading metadata into new headings", () => {
  assert.equal(sourceMarkdown({
    text: null,
    spans: [
      span(0, "Long section", "#### Long section\n\nFirst chunk."),
      span(1, "Long section", "Continuation."),
      span(2, "Page 1", "PDF text without a Markdown heading.")
    ]
  }), "#### Long section\n\nFirst chunk.\n\nContinuation.\n\nPDF text without a Markdown heading.");
});

test("unparsed sources fall back to the original text without fabricating content", () => {
  assert.equal(sourceMarkdown({ text: "# Original\n\nText.", spans: [] }), "# Original\n\nText.");
  assert.equal(sourceMarkdown({ text: null, spans: [] }), "");
  assert.equal(sourceMarkdown({ text: "Original HTML", spans: [span(0, null, "Extracted text.")] }),
    "Extracted text.");
});
