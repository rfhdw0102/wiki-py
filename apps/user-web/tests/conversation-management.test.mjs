import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import ts from "typescript";
import {
  createQuestionOperation, upsertQuestionConversation, withQuestionStorage
} from "../src/app/search/question-operation.ts";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/app/search/page.tsx", import.meta.url), "utf8");
const code = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
}).outputText;
const tick = () => new Promise((resolve) => setImmediate(resolve));

function find(root, predicate) {
  if (!React.isValidElement(root)) return undefined;
  if (predicate(root)) return root;
  for (const child of React.Children.toArray(root.props.children)) {
    const matched = find(child, predicate);
    if (matched) return matched;
  }
  return undefined;
}

function harness() {
  let cursor = 0;
  const hooks = [], effects = [], submissions = [], renameRequests = [], storage = new Map();
  let conversations = [];
  let lookup = null;
  let failRename = false;
  let askResolve;
  const askPromise = new Promise((resolve) => { askResolve = resolve; });
  const userWiki = "test-wiki";
  const api = {
    listQuestionConversations: async () => conversations,
    askWiki: (query, conversationId, requestId) => {
      submissions.push({ query, conversationId, requestId });
      return askPromise;
    },
    getQuestionRequest: async () => lookup,
    getQuestionRun: async () => lookup,
    getQuestionConversation: async (id) => ({
      id, title: conversations.find(item => item.id === id)?.title, messages: []
    }),
    renameQuestionConversation: async (id, title) => {
      renameRequests.push({ id, title });
      if (failRename) throw new Error("模拟改名失败");
      const updated = { ...conversations.find(item => item.id === id), title };
      conversations = [updated];
      return updated;
    }
  };
  const exports = {};
  runInNewContext(code, {
    exports, Error, Map, Date, Promise, AbortSignal,
    crypto: { randomUUID: (() => { let id = 0; return () => `request-${++id}`; })() },
    window: {
      localStorage: {
        getItem: key => storage.get(key) ?? null,
        setItem: (key, value) => storage.set(key, value),
        removeItem: key => storage.delete(key)
      },
      addEventListener: () => {}, removeEventListener: () => {},
      setTimeout
    },
    require: (name) => {
      if (name === "react") return {
        ...React,
        useState: (initial) => {
          const index = cursor++;
          if (!(index in hooks)) hooks[index] = initial;
          return [hooks[index], (value) => {
            hooks[index] = typeof value === "function" ? value(hooks[index]) : value;
          }];
        },
        useRef: (initial) => {
          const index = cursor++;
          if (!(index in hooks)) hooks[index] = { current: initial };
          return hooks[index];
        },
        useEffect: (effect, deps) => effects.push({ effect, deps }),
        useCallback: callback => callback
      };
      if (name === "@wiki-agent/web-ui") return {
        api, getActiveWikiId: () => userWiki,
        formatDateTime: value => value, RequestState: () => null, StatusBadge: () => null,
        useApiResource: () => ({
          data: [{ key: "wiki_questions", enabled: true }], error: null,
          loading: false, reload: () => {}
        })
      };
      if (name === "./question-operation") return {
        createQuestionOperation: options => createQuestionOperation({
          ...options, attempts: 1
        }),
        upsertQuestionConversation, withQuestionStorage,
        parsePendingQuestions: value => JSON.parse(value ?? "[]"),
        restorePendingTurns: turns => turns,
        cancelledQuestion: status => status === "cancelled",
        completedQuestion: status => status === "completed",
        failedQuestion: status => status === "failed"
      };
      if (name === "./answer-message") return { AnswerMessage: () => null };
      return require(name);
    }
  });
  return {
    api, submissions, renameRequests,
    setConversations: value => { conversations = value; },
    setLookup: value => { lookup = value; },
    setFailRename: value => { failRename = value; },
    completeAsk: value => askResolve(value),
    render: () => { cursor = 0; effects.length = 0; return exports.default(); },
    mount: () => effects.find(item => item.deps?.length === 0).effect()
  };
}

test("the new conversation button is beside history, not in the answer header", async () => {
  const h = harness();
  h.render();
  h.mount();
  await tick();
  const tree = h.render();
  const header = find(tree, el => el.props.className === "chat-workspace-head");
  const historyHeader = find(tree, el => el.props.className === "chat-history-head");
  assert.equal(find(header, el => el.props["aria-label"] === "新对话"), undefined);
  assert.ok(find(historyHeader, el => el.props["aria-label"] === "新对话"));
});

test("a first question automatically requests a new conversation without a prior plus click", async () => {
  const h = harness();
  h.render();
  h.mount();
  await tick();
  let tree = h.render();
  const questionForm = find(tree, el => el.type === "form" && el.props.className === "chat-composer");
  find(questionForm, el => el.type === "textarea").props.onChange({ target: { value: "首次提问" } });
  tree = h.render();
  find(tree, el => el.props.className === "chat-composer").props.onSubmit({ preventDefault() {} });
  await tick();
  assert.equal(h.submissions.length, 1);
  assert.equal(h.submissions[0].query, "首次提问");
  assert.equal(h.submissions[0].conversationId, undefined);
  assert.ok(h.submissions[0].requestId);
  h.setConversations([{
    id: "auto-created", title: "首次提问标题",
    created_at: "2026-10-10T03:00:00Z", updated_at: "2026-10-10T03:00:00Z"
  }]);
  h.completeAsk({
    answer: "回答", citations: [], uncertainty: null, insufficient_evidence: true,
    degradations: [], evidence: [], conversation_id: "auto-created",
    conversation_title: "首次提问标题", run_id: "auto-run", message_id: "message",
    clarification_question: null
  });
  await tick();
  tree = h.render();
  assert.ok(find(tree, el => el.props.className === "chat-history-select"
    && el.props.title === "首次提问标题"));
});

test("clicking the title edits inline and Enter persists it in the header and history", async () => {
  const h = harness();
  h.setConversations([{
    id: "conversation", title: "旧名称",
    created_at: "2026-10-10T03:00:00Z", updated_at: "2026-10-10T03:00:00Z"
  }]);
  h.render();
  h.mount();
  await tick();
  let tree = h.render();
  assert.equal(find(tree, el => el.props.className === "chat-history-rename"), undefined);
  find(tree, el => el.props.className === "chat-conversation-title"
    && el.type === "button").props.onClick();
  tree = h.render();
  assert.equal(find(tree, el => el.props.role === "dialog"), undefined);
  const input = find(tree, el => el.props.className === "chat-conversation-title-input");
  input.props.onChange({ target: { value: "  新名称  " } });
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input")
    .props.onKeyDown({ key: "Enter", preventDefault() {} });
  await tick();
  tree = h.render();
  assert.deepEqual(h.renameRequests, [{ id: "conversation", title: "新名称" }]);
  assert.equal(find(tree, el => el.props.className === "chat-conversation-title-input"), undefined);
  assert.ok(find(tree, el => el.props.className === "chat-history-select"
    && el.props.title === "新名称"));
  const header = find(tree, el => el.props.className === "chat-conversation-head");
  assert.equal(find(header, el => el.type === "button").props.children, "新名称");
});

test("failed conversation rename remains editable with an explicit error", async () => {
  const h = harness();
  h.setConversations([{
    id: "conversation", title: "旧名称",
    created_at: "2026-10-10T03:00:00Z", updated_at: "2026-10-10T03:00:00Z"
  }]);
  h.setFailRename(true);
  h.render();
  h.mount();
  await tick();
  let tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title"
    && el.type === "button").props.onClick();
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input")
    .props.onChange({ target: { value: "新名称" } });
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input").props.onBlur();
  await tick();
  tree = h.render();
  const failed = find(tree, el => el.props.className === "chat-conversation-head");
  assert.equal(find(failed, el => el.props.role === "alert").props.children, "模拟改名失败");
  assert.equal(find(failed, el => el.type === "input").props.disabled, false);
  assert.ok(find(tree, el => el.props.className === "chat-history-select"
    && el.props.title === "旧名称"));
});

test("a late question response cannot overwrite a manually renamed conversation", async () => {
  const h = harness();
  h.setConversations([{
    id: "conversation", title: "旧名称",
    created_at: "2026-10-10T03:00:00Z", updated_at: "2026-10-10T03:00:00Z"
  }]);
  h.setLookup({
    id: "run", conversation_id: "conversation", conversation_title: "旧名称",
    status: "running", stage: "answering", error: null, resumable: false, answer: null
  });
  h.render();
  h.mount();
  await tick();
  let tree = h.render();
  find(tree, el => el.type === "textarea").props.onChange({ target: { value: "问题" } });
  tree = h.render();
  find(tree, el => el.props.className === "chat-composer").props.onSubmit({ preventDefault() {} });
  await tick();
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title"
    && el.type === "button").props.onClick();
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input")
    .props.onChange({ target: { value: "我的新名称" } });
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input")
    .props.onKeyDown({ key: "Enter", preventDefault() {} });
  await tick();
  h.completeAsk({
    answer: "回答", conversation_id: "conversation", conversation_title: "旧名称",
    run_id: "run", clarification_question: null
  });
  await tick();
  tree = h.render();
  assert.ok(find(tree, el => el.props.className === "chat-history-select"
    && el.props.title === "我的新名称"));
});

test("Escape discards an inline title edit even if blur follows", async () => {
  const h = harness();
  h.setConversations([{
    id: "conversation", title: "旧名称",
    created_at: "2026-10-10T03:00:00Z", updated_at: "2026-10-10T03:00:00Z"
  }]);
  h.render();
  h.mount();
  await tick();
  let tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title"
    && el.type === "button").props.onClick();
  tree = h.render();
  find(tree, el => el.props.className === "chat-conversation-title-input")
    .props.onChange({ target: { value: "不保存的名称" } });
  tree = h.render();
  const input = find(tree, el => el.props.className === "chat-conversation-title-input");
  input.props.onKeyDown({ key: "Escape", preventDefault() {}, stopPropagation() {} });
  input.props.onBlur();
  await tick();
  tree = h.render();
  assert.deepEqual(h.renameRequests, []);
  assert.equal(find(tree, el => el.props.className === "chat-conversation-title-input"), undefined);
  assert.equal(find(tree, el => el.props.className === "chat-conversation-title").props.children, "旧名称");
});
