import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

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
