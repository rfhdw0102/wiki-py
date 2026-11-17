import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import {
  cancelledQuestion,
  completedQuestion,
  createQuestionOperation,
  parsePendingQuestions,
  restorePendingTurns,
  upsertQuestionConversation,
  withQuestionStorage
} from "../src/app/search/question-operation.ts";

const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const run = (status = "running", extra = {}) => ({
  id: "run-1", request_id: "request-1", question: "Question",
  conversation_id: "conversation-1", conversation_title: "Generated title",
  status, stage: "retrieval", error: null, resumable: false,
  answer: status === "completed" ? { answer: "Answer", citations: [] } : null,
  ...extra
});
const submission = {
  answer: "Answer", conversation_id: "conversation-1", conversation_title: "Generated title",
  run_id: "run-1", clarification_question: null
};

async function renderPageFragment(select) {
  const page = await readFile(new URL("../src/app/search/page.tsx", import.meta.url), "utf8");
  const source = ts.createSourceFile("page.tsx", page, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let fragment;
  function visit(node) {
    if (select(node)) fragment = node;
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(fragment, "The tested JSX must come from the actual page");
  const code = ts.transpileModule(
    `exports.Fragment = function ({turn, loading, storageError}) { return (${fragment.getText(source)}); };`,
    { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }
  ).outputText;
  const exports = {};
  runInNewContext(code, {
    exports, require: createRequire(import.meta.url),
    cancelledQuestion, completedQuestion, copiedTurnId: null,
    AnswerMessage: () => null
  });
  return (props) => renderToStaticMarkup(React.createElement(exports.Fragment, props));
}

const isAssistantBubble = (node) => ts.isJsxElement(node)
  && node.openingElement.attributes.properties.some((attribute) =>
    ts.isJsxAttribute(attribute) && attribute.name.getText() === "className"
    && attribute.initializer && ts.isStringLiteral(attribute.initializer)
    && attribute.initializer.text === "chat-bubble chat-bubble-assistant"
  );

function harness(overrides = {}) {
  const events = { runs: [], submissions: [], errors: [], cancelErrors: [], busy: [], cancelling: [] };
  const operation = createQuestionOperation({
    lookup: async () => run("completed"),
    cancel: async () => run("cancelled"),
    isCurrent: () => true,
    onRun: (value) => events.runs.push(value),
    onSubmission: (value) => events.submissions.push(value),
    onError: (value) => events.errors.push(value),
    onCancelError: (value) => events.cancelErrors.push(value),
    onBusy: (value) => events.busy.push(value),
    onCancelling: (value) => events.cancelling.push(value),
    attempts: 3,
    delay: tick,
    ...overrides
  });
  return { operation, events };
}

test("request polling discovers the run and title and permits cancel before synchronous POST returns", async () => {
  const post = deferred(), delay = deferred();
  const lookups = [], cancellations = [];
  let attempt = 0;
  const { operation, events } = harness({
    submit: () => post.promise,
    lookup: async (id) => { lookups.push(id); return attempt++ ? run() : null; },
    delay: async () => { if (attempt > 1) await delay.promise; },
    cancel: async (id) => { cancellations.push(id); return run("cancelled"); }
  });
  const polling = operation.start();
  await tick();
  assert.deepEqual(lookups, [undefined, undefined]);
  assert.equal(events.runs[0].conversation_title, "Generated title");
  await operation.cancel();
  assert.deepEqual(cancellations, ["run-1"]);
  assert.equal(events.runs.at(-1).status, "cancelled");
  post.reject(new Error("late network failure"));
  delay.resolve();
  await polling;
  await tick();
  assert.deepEqual(events.errors, []);
  assert.equal(events.busy.at(-1), false);
});

test("resume polls the existing run throughout the POST and completed poll defeats its late failure", async () => {
  const post = deferred();
  let attempt = 0;
  const ids = [];
  const { operation, events } = harness({
    runId: "run-1",
    submit: () => post.promise,
    lookup: async (id) => {
      ids.push(id);
      return [run("failed", { resumable: true }), run(), run("completed")][attempt++];
    }
  });
  await operation.start();
  post.reject(new Error("late POST failure"));
  await tick();
  assert.deepEqual(ids, ["run-1", "run-1", "run-1"]);
  assert.equal(events.runs.at(-1).answer.answer, "Answer");
  assert.deepEqual(events.errors, []);
  assert.equal(events.busy.at(-1), false);
});

test("full successful POST is final and a stale lookup or cancellation cannot replace it", async () => {
  const lookup = deferred();
  let cancels = 0;
  const { operation, events } = harness({
    submit: async () => submission,
    lookup: () => lookup.promise,
    cancel: async () => { cancels++; return run("cancelled"); }
  });
  const polling = operation.start();
  await tick();
  lookup.resolve(run("failed"));
  await polling;
  await operation.cancel();
  assert.deepEqual(events.submissions, [submission]);
  assert.deepEqual(events.runs, []);
  assert.equal(cancels, 0);
});

test("confirmed cancellation discards a late successful POST", async () => {
  const post = deferred();
  const { operation, events } = harness({
    submit: () => post.promise, lookup: async () => run("cancelled")
  });
  await operation.start();
  post.resolve(submission);
  await tick();
  assert.deepEqual(events.submissions, []);
  assert.equal(events.runs.at(-1).status, "cancelled");
});

test("completion wins a cancel race, even when the stale cancel response claims cancelled", async () => {
  const post = deferred(), cancellation = deferred();
  const { operation, events } = harness({
    runId: "run-1", attempts: 1,
    submit: () => post.promise,
    lookup: async () => run(),
    cancel: () => cancellation.promise
  });
  await operation.start();
  const cancelling = operation.cancel();
  post.resolve(submission);
  await tick();
  cancellation.resolve(run("cancelled"));
  await cancelling;
  assert.deepEqual(events.submissions, [submission]);
  assert.deepEqual(events.runs.map((value) => value.status), ["running"]);
  assert.equal(events.busy.at(-1), false);
});

test("cancellation errors are explicit and the user can retry without marking the run cancelled", async () => {
  let calls = 0;
  const { operation, events } = harness({
    runId: "run-1", attempts: 1, lookup: async () => run(),
    cancel: async () => {
      if (!calls++) throw new Error("connection lost");
      return run("cancelled");
    }
  });
  await operation.start();
  await operation.cancel();
  assert.equal(events.cancelErrors.at(-1), "connection lost");
  assert.equal(events.runs.at(-1).status, "running");
  await operation.cancel();
  assert.equal(events.runs.at(-1).status, "cancelled");
  assert.deepEqual(events.cancelling, [true, false, true, false]);
});

test("canceling an already-completed server run renders its answer, not a cancelled turn", async () => {
  const { operation, events } = harness({
    runId: "run-1", attempts: 1,
    lookup: async () => run(),
    cancel: async () => run("completed")
  });
  await operation.start();
  await operation.cancel();
  assert.equal(events.runs.at(-1).status, "completed");
  assert.equal(events.runs.at(-1).answer.answer, "Answer");
});

test("lookup failures stop polling and expose the error without losing the POST's later completion", async () => {
  const post = deferred();
  let lookups = 0;
  const { operation, events } = harness({
    submit: () => post.promise,
    lookup: async (_id, signal) => {
      assert.ok(signal instanceof AbortSignal);
      lookups++;
      throw new Error("lookup timed out");
    }
  });
  await operation.start();
  assert.equal(lookups, 1);
  assert.deepEqual(events.errors, ["lookup timed out"]);
  assert.equal(events.busy.at(-1), false);
  post.resolve(submission);
  await tick();
  assert.deepEqual(events.submissions, [submission]);
});

test("polling is bounded and never automatically resubmits an unresolved request", async () => {
  const post = deferred();
  let lookups = 0, posts = 0;
  const { operation, events } = harness({
    submit: () => { posts++; return post.promise; },
    lookup: async () => { lookups++; return null; }
  });
  await operation.start();
  assert.equal(lookups, 3);
  assert.equal(posts, 1);
  assert.match(events.errors.at(-1), /手动/);
  assert.equal(events.busy.at(-1), false);
  post.reject(new Error("network disconnected"));
  await tick();
  assert.equal(posts, 1);
});

test("a lost POST response is recovered through lookup without submitting again", async () => {
  let posts = 0, lookups = 0;
  const { operation, events } = harness({
    submit: async () => { posts++; throw new Error("lost response"); },
    lookup: async () => ++lookups === 1 ? run() : run("completed")
  });
  await operation.start();
  assert.equal(posts, 1);
  assert.equal(events.runs.at(-1).status, "completed");
});

test("version invalidation suppresses both lookup and POST results", async () => {
  const post = deferred(), lookup = deferred();
  let current = true;
  const { operation, events } = harness({
    submit: () => post.promise,
    lookup: () => lookup.promise,
    isCurrent: () => current
  });
  const polling = operation.start();
  current = false;
  post.resolve(submission);
  lookup.resolve(run("completed"));
  await polling;
  await tick();
  assert.deepEqual(events.runs, []);
  assert.deepEqual(events.submissions, []);
  assert.deepEqual(events.errors, []);
});

test("server resumability releases recovery immediately for failed or stale running work", async () => {
  for (const status of ["failed", "running"]) {
    let lookups = 0;
    const { operation, events } = harness({
      lookup: async () => { lookups++; return run(status, { resumable: true }); }
    });
    await operation.start();
    assert.equal(lookups, 1);
    assert.equal(events.busy.at(-1), false);
    assert.equal(events.runs[0].resumable, true);
  }
});

test("hung POST releases failed or resumable work after one short polling grace without clearing pending work", async () => {
  for (const status of ["failed", "running"]) {
    const post = deferred();
    let lookups = 0;
    const { operation, events } = harness({
      attempts: 60,
      submit: () => post.promise,
      lookup: async () => { lookups++; return run(status, { resumable: true }); }
    });
    await operation.start();
    assert.equal(lookups, 2);
    assert.equal(events.busy.at(-1), false);
    assert.equal(events.runs.at(-1).resumable, true);
    assert.deepEqual(events.submissions, []);
    assert.deepEqual(events.errors, []);
    // The outstanding POST is not cancelled or replayed by the status grace.
    post.resolve(submission);
    await tick();
    assert.deepEqual(events.submissions, [submission]);
  }
});

test("resume's first old failed checkpoint remains busy until the next fresh running checkpoint", async () => {
  const post = deferred(), grace = deferred();
  let lookups = 0;
  const { operation, events } = harness({
    runId: "run-1",
    submit: () => post.promise,
    lookup: async () => {
      lookups++;
      return lookups === 1 ? run("failed", { resumable: true })
        : lookups === 2 ? run() : run("completed");
    },
    delay: () => lookups === 1 ? grace.promise : tick()
  });
  const polling = operation.start();
  await tick();
  assert.equal(lookups, 1);
  assert.equal(events.busy.at(-1), true);
  grace.resolve();
  await polling;
  assert.equal(lookups, 3);
  assert.equal(events.runs.at(-1).status, "completed");
  post.reject(new Error("stale POST error"));
  await tick();
  assert.deepEqual(events.errors, []);
});

test("actual page JSX renders stop during normal loading and resume without a question error", async () => {
  const render = await renderPageFragment(isAssistantBubble);
  const turn = { id: "turn", runId: "run-1", status: "running", loading: true };
  const running = render({ turn, loading: true });
  assert.match(running, /<button(?![^>]*disabled)[^>]*>停止运行<\/button>/);
  assert.ok(!running.includes('role="alert"'));

  const resumable = render({
    turn: { ...turn, loading: false, resumable: true }, loading: false
  });
  assert.match(resumable, /<button(?![^>]*disabled)[^>]*>恢复运行<\/button>/);
  assert.ok(!resumable.includes('role="alert"'));

  const failedCancel = render({
    turn: { ...turn, cancelError: "network disconnected" }, loading: true
  });
  assert.match(failedCancel, /role="alert"[^>]*>停止请求失败：network disconnected/);

  const undiscovered = render({ turn: { loading: true }, loading: true });
  assert.match(undiscovered, /<button[^>]*disabled[^>]*>等待运行登记…<\/button>/);
  for (const status of ["cancelled", "completed"]) {
    const terminal = render({ turn: { ...turn, status, loading: false }, loading: false });
    assert.ok(!terminal.includes("停止运行"));
    assert.ok(!terminal.includes("恢复运行"));
  }
});

test("storage failures notify the user for reads, writes and removal without breaking active operations", async () => {
  for (const method of ["getItem", "setItem", "removeItem"]) {
    const messages = [];
    const storage = { [method]() { throw new Error("storage blocked"); } };
    assert.equal(withQuestionStorage(() => storage[method]("key"), (message) => messages.push(message)), undefined);
    assert.equal(messages.length, 1);
    assert.match(messages[0], /刷新后可能无法恢复/);
    const render = await renderPageFragment((node) =>
      ts.isConditionalExpression(node) && ts.isIdentifier(node.condition)
      && node.condition.text === "storageError"
    );
    assert.match(render({ storageError: messages[0] }), /role="alert"/);
    assert.match(render({ storageError: messages[0] }), /检查浏览器存储设置/);
    assert.equal(render({ storageError: null }), "");
  }
  const messages = [];
  assert.equal(withQuestionStorage(() => "saved", (message) => messages.push(message)), "saved");
  assert.deepEqual(messages, []);
});

test("pending submissions restore before a run or conversation ID is known and migrate the previous format", () => {
  const pending = { requestId: "stable-request", question: "Question", turnId: "turn" };
  assert.deepEqual(parsePendingQuestions(JSON.stringify([pending])), [pending]);
  assert.deepEqual(parsePendingQuestions(JSON.stringify({
    runId: "legacy-run", question: "Question", turnId: "legacy-turn", conversationId: "conversation"
  }))[0].requestId, "legacy-run");
  assert.deepEqual(parsePendingQuestions(JSON.stringify([null, {}, { requestId: 12 }])), []);
});

test("reload retains failed, running and cancelled turns instead of confusing repeated questions with old success", () => {
  const history = [{
    id: "old-success", question: "Question", loading: false,
    answer: { answer: "Old answer" }, createdAt: "2026-10-01T00:00:00Z"
  }];
  const pending = ["failed", "running", "cancelled"].map((status, index) => ({
    requestId: `request-${index}`, runId: `run-${index}`, turnId: `turn-${index}`,
    question: "Question", status, resumable: true, error: "saved error",
    createdAt: `2026-10-0${index + 2}T00:00:00Z`
  }));
  const turns = restorePendingTurns(history, pending);
  assert.equal(turns.length, 4);
  assert.equal(turns[0].answer.answer, "Old answer");
  assert.deepEqual(turns.slice(1).map((value) => value.status), ["failed", "running", "cancelled"]);
  assert.equal(turns.at(-1).resumable, false);
  assert.match(turns.at(-1).error, /无法恢复/);
});

test("a newly discovered conversation is inserted and its generated title replaces the placeholder", () => {
  const initial = upsertQuestionConversation([], "new", "Placeholder", "2026-10-01");
  const updated = upsertQuestionConversation(initial, "new", "Generated title", "2026-10-02");
  assert.equal(updated.length, 1);
  assert.equal(updated[0].title, "Generated title");
  assert.equal(updated[0].created_at, "2026-10-01");
});

test("page persists before submission and recovery uses the original request lookup and ID", async () => {
  const page = await readFile(new URL("../src/app/search/page.tsx", import.meta.url), "utf8");
  const send = page.slice(page.indexOf("async function sendQuestion"), page.indexOf("function recoverRun"));
  assert.ok(send.indexOf("savePendingRun(") < send.indexOf("api.getQuestionRequest("));
  assert.ok(send.indexOf("api.getQuestionRequest(") < send.indexOf("api.askWiki("));
  assert.match(send, /question, pending\?\.conversationId[\s\S]*requestId/);
  assert.match(page, /!turn\.resumable \|\| cancelledQuestion/);
  assert.match(page, /停止请求失败/);
  const api = await readFile(new URL("../../../packages/web-ui/src/api.ts", import.meta.url), "utf8");
  assert.match(api, /getQuestionRequest[\s\S]*requests\/\$\{encodeURIComponent\(requestId\)\}/);
  assert.match(api, /cancelQuestionRun[\s\S]*runs\/\$\{encodeURIComponent\(id\)\}\/cancel/);
});
