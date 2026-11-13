import assert from "node:assert/strict";
import test from "node:test";
import { browserPoint, browserPointerAction } from "../src/app/sources/browser-controls.ts";
import { crawlImportSummary, websiteImportPayload } from "../src/app/sources/website-import.ts";

test("URL imports default to a single article and site crawling is an explicit choice", () => {
  assert.deepEqual(websiteImportPayload(" https://example.com/article#section ", "page"), {
    url: "https://example.com/article", max_depth: 0, max_pages: 1
  });
  assert.deepEqual(websiteImportPayload("https://example.com", "site"), {
    url: "https://example.com/", max_depth: 1, max_pages: 20
  });
  for (const value of ["invalid", "file:///private/test", "https://user:password@example.com"]) {
    assert.throws(() => websiteImportPayload(value, "page"));
  }
});

test("zero imported articles are never displayed as success even for old completed runs", () => {
  const result = crawlImportSummary({
    status: "completed", input_json: { url: "https://example.com/article" },
    result_json: {
      source_revision_ids: [], errors: [{
        url: "https://example.com/article", code: "verification_required", error: "Internal details"
      }]
    }
  });
  assert.equal(result.failed, true);
  assert.match(result.message, /没有导入有效正文/);
  assert.equal(result.browserUrl, "https://example.com/article");
  assert.match(result.errors[0].message, /手动验证/);
  assert.ok(!JSON.stringify(result).includes("Internal details"));
});

test("partial crawl failures show imported counts and individual page failures", () => {
  const result = crawlImportSummary({
    status: "completed_with_errors", input_json: { url: "https://example.com/" },
    result_json: {
      source_revision_ids: ["revision-1", "revision-2"],
      imported_count: 2, failed_count: 1,
      errors: [{ url: "https://example.com/private", code: "login_required" }]
    }
  });
  assert.match(result.message, /2 篇正文，1 个页面未导入/);
  assert.equal(result.failed, true);
  assert.equal(result.browserUrl, "https://example.com/private");
});

test("a successful import is a saved article, not a published Wiki page", () => {
  const result = crawlImportSummary({
    status: "completed", input_json: {},
    result_json: { source_revision_ids: ["revision-1"], imported_count: 1, failed_count: 0 }
  });
  assert.equal(result.failed, false);
  assert.match(result.message, /原件已保存，可手动编译/);
  assert.equal(result.browserUrl, null);
});

test("remote pointer coordinates account for screenshot scaling and stay within the viewport", () => {
  const bounds = { left: 100, top: 50, width: 512, height: 384 };
  assert.deepEqual(browserPoint(356, 242, bounds, 1024, 768), { x: 512, y: 384 });
  assert.deepEqual(browserPoint(99, 49, bounds, 1024, 768), { x: 0, y: 0 });
  assert.deepEqual(browserPoint(700, 500, bounds, 1024, 768), { x: 1023, y: 767 });
  assert.throws(() => browserPoint(0, 0, { ...bounds, width: 0 }, 1024, 768));
});

test("manual slider gestures are distinguished from clicks without automatic solving", () => {
  assert.deepEqual(browserPointerAction({ x: 10, y: 20 }, { x: 13, y: 24 }), {
    action: "click", x: 10, y: 20
  });
  assert.deepEqual(browserPointerAction({ x: 10, y: 20 }, { x: 80, y: 20 }), {
    action: "drag", x: 10, y: 20, to_x: 80, to_y: 20
  });
});
