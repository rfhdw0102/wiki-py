import assert from "node:assert/strict";
import test from "node:test";
import { sourceDeletionErrorMessage } from "../src/app/sources/source-deletion.ts";

test("referenced sources are not incorrectly reported as a changed Wiki", () => {
  assert.equal(
    sourceDeletionErrorMessage("Source is referenced by Wiki pages; remove the references first", 409),
    "此来源仍被 Wiki 页面引用，删除未完成。"
  );
  assert.equal(
    sourceDeletionErrorMessage("Source is referenced by a draft; remove the draft first", 409),
    "此来源仍被草稿引用，删除未完成。"
  );
});

test("active processing and an actual stale Wiki have distinct deletion errors", () => {
  assert.equal(
    sourceDeletionErrorMessage("Source is still being processed", 409),
    "资料仍在处理，完成后才能删除。"
  );
  assert.equal(
    sourceDeletionErrorMessage("Wiki changed; retry source deletion", 409),
    "Wiki 内容已更新，请重试删除。"
  );
  assert.equal(sourceDeletionErrorMessage("Different conflict", 409), "Different conflict");
});

test("partial deletion failures are explicit and unknown errors are preserved", () => {
  assert.equal(
    sourceDeletionErrorMessage("Source deleted, but object cleanup failed", 500),
    "来源已移除，但原始文件清理失败，请联系管理员检查。"
  );
  assert.equal(
    sourceDeletionErrorMessage("Wiki cleanup completed, but source deletion failed; refresh and retry", 500),
    "Wiki 内容已清理，但来源记录删除失败，请刷新后重试。"
  );
  assert.equal(
    sourceDeletionErrorMessage("Source deletion failed; refresh and retry", 500),
    "删除未完成，请刷新后重试。"
  );
  assert.equal(sourceDeletionErrorMessage("Failed to fetch"), "Failed to fetch");
});
