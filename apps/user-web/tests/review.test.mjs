import assert from "node:assert/strict";
import test from "node:test";
import {
  openDrafts, reviewExplanationError, reviewFailureMessage, reviewReasons
} from "../src/app/conflicts/review.ts";

test("publishing accepts an optional explanation while rejection still needs a reason", () => {
  for (const explanation of ["", "   ", "好", "更新来源摘要"]) {
    assert.equal(reviewExplanationError("publish", explanation), null);
  }
  for (const explanation of ["", "   ", "否", " 不要 "]) {
    assert.match(reviewExplanationError("reject", explanation), /至少 3 个字符/);
  }
  assert.equal(reviewExplanationError("reject", " 来源不可靠 "), null);
});

test("rejection explains protected references and partial file cleanup failures", () => {
  assert.match(reviewFailureMessage(
    "Source is referenced by Wiki pages; remove the references first"
  ), /已被发布.*草稿和原件均保留/);
  assert.match(reviewFailureMessage(
    "Source is referenced by a draft; remove the draft first"
  ), /其他草稿.*草稿和原件均保留/);
  assert.match(reviewFailureMessage("Source is still being processed"), /等待处理结束/);
  assert.match(reviewFailureMessage("Source deleted, but object cleanup failed"), /文件清理失败/);
  assert.equal(reviewFailureMessage("服务暂时不可用"), "服务暂时不可用");
});
test("only drafts awaiting a decision appear in the review list", () => {
  const drafts = [
    { id: "open", status: "open" },
    { id: "published", status: "published" },
    { id: "rejected", status: "rejected" }
  ];
  assert.deepEqual(openDrafts(drafts).map((draft) => draft.id), ["open"]);
});

test("conflicts and unrelated review risks are explained independently", () => {
  const draft = { id: "d", files: { "sources/source.md": "content" } };
  const runs = [{
    kind: "compile_source",
    result_json: {
      draft_id: "d",
      risk_violations: ["too_many_changed_pages", "unresolved_conflicts"]
    }
  }];
  assert.deepEqual(reviewReasons(draft, runs), [
    "改动页面过多", "存在待处理冲突"
  ]);
  assert.deepEqual(reviewReasons(
    { ...draft, files: { "pending/conflict.md": "content" } }, []
  ), ["存在待处理冲突"]);
  assert.deepEqual(reviewReasons(draft, []), []);
});
