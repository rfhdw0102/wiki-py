import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const code = ts.transpileModule(readFileSync(
  new URL("../../../packages/web-ui/src/api.ts", import.meta.url), "utf8"
), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;

test("batch API sends repeated multipart files and one compile admission payload", async () => {
  const requests = [];
  const response = {
    items: [], compilation_run_id: "shared", compilation_status: "queued", compilation_error: null
  };
  const exports = {};
  runInNewContext(code, {
    exports, FormData, Headers, process: { env: {} },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, status: 200, json: async () => response };
    }
  });
  const files = [new File(["one"], "one.md"), new File(["two"], "two.txt")];
  assert.equal(await exports.api.uploadSourceBatch(files), response);
  const upload = requests[0];
  assert.ok(upload.url.endsWith("/sources/batch"));
  assert.equal(upload.options.method, "POST");
  assert.deepEqual(upload.options.body.getAll("files").map((file) => file.name), ["one.md", "two.txt"]);
  assert.equal(upload.options.body.has("file"), false);
  assert.equal(upload.options.headers.has("content-type"), false);
  await exports.api.compileSourceBatch(["source-one", "source-two"]);
  const compile = requests[1];
  assert.ok(compile.url.endsWith("/sources/compile-batch"));
  assert.equal(compile.options.method, "POST");
  assert.deepEqual(JSON.parse(compile.options.body), { source_ids: ["source-one", "source-two"] });
  assert.equal(requests.length, 2);
});
