"use client";

import {
  RequestState,
  StatusBadge,
  api,
  formatDateTime,
  getActiveWikiId,
  type WikiQuestionConversation,
  type WikiQuestionConversationSummary,
  type WikiQuestionResponse,
  useApiResource
} from "@wiki-agent/web-ui";
import {
  type FormEvent,
  type KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState
} from "react";
import {
  cancelledQuestion,
  completedQuestion,
  createQuestionOperation,
  failedQuestion,
  parsePendingQuestions,
  restorePendingTurns,
  upsertQuestionConversation,
  withQuestionStorage,
  type ConversationTurn,
  type PendingQuestion
} from "./question-operation";

const conversationKey = (wikiId: string) => `wiki-question-conversation:${wikiId}`;
const pendingRunKey = (wikiId: string) => `wiki-question-pending-run:${wikiId}`;
const newRequestId = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

function conversationTurns(conversation: WikiQuestionConversation): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (const message of conversation.messages) {
    if (message.role === "user") {
      turns.push({ id: message.id, question: message.content, loading: false, createdAt: message.created_at });
    } else if (message.role === "assistant") {
      const last = turns[turns.length - 1];
      const turn = last && !last.answer && !last.text
        ? last
        : { id: message.id, question: "", loading: false };
      turn.answer = message.answer ?? undefined;
      turn.text = message.answer ? undefined : message.content;
      turn.citations = message.citations;
      if (turn !== last) turns.push(turn);
    }
  }
  return turns;
}

const SUGGESTED_QUESTIONS = [
  "总结当前知识库的核心内容",
  "最近有哪些重要更新？",
  "哪些结论存在证据不足？"
];

export default function SearchPage() {
  const loadFeatures = useCallback(() => api.listFeatures(), []);
  const {
    data: features,
    error: featureError,
    loading: featuresLoading,
    reload: reloadFeatures
  } = useApiResource(loadFeatures);
  const [wikiId, setWikiId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<WikiQuestionConversationSummary[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<WikiQuestionConversationSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [turns, setTurns] = useState<ConversationTurn[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [copiedTurnId, setCopiedTurnId] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [storageError, setStorageError] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<WikiQuestionConversationSummary | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState(false);
  const renamingRef = useRef(false);
  const titleEditCancelledRef = useRef(false);
  const customTitlesRef = useRef(new Map<string, string>());
  const chatMessagesRef = useRef<HTMLDivElement>(null);
  const activeConversationRef = useRef<string | null>(null);
  const selectionVersion = useRef(0);
  const operationVersion = useRef(0);
  const loadingRef = useRef(false);
  const postingRef = useRef(false);
  const switchingRef = useRef(true);
  const deletingRef = useRef(false);
  const questionOperationRef = useRef<{
    turnId: string;
    operation: ReturnType<typeof createQuestionOperation>;
  } | null>(null);
  const questionsEnabled =
    features?.some((flag) => flag.key === "wiki_questions" && flag.enabled) ?? false;

  function cancelTitleEdit() {
    if (renamingRef.current) return;
    titleEditCancelledRef.current = true;
    setRenameTarget(null);
    setRenameError(null);
  }

  useEffect(() => {
    if (!renameTarget) return;
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") cancelTitleEdit();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [renameTarget]);

  async function saveConversationTitle() {
    if (!renameTarget || renamingRef.current || titleEditCancelledRef.current) return;
    const title = renameTitle.trim();
    if (!title || title.length > 120 || /[\u0000-\u001f\u007f\u2028\u2029]/.test(title)) {
      setRenameError("对话名称应为 1–120 个字符，不能包含换行或控制字符。");
      return;
    }
    const targetId = renameTarget.id;
    if (title === renameTarget.title) {
      setRenameTarget(null);
      setRenameError(null);
      return;
    }
    renamingRef.current = true;
    setRenaming(true);
    setRenameError(null);
    try {
      const updated = await api.renameQuestionConversation(targetId, title);
      customTitlesRef.current.set(targetId, updated.title);
      setConversations((current) => [
        updated, ...current.filter((item) => item.id !== updated.id)
      ]);
      if (wikiId) {
        for (const pending of readPendingRuns(wikiId)) {
          if (pending.conversationId === targetId) {
            savePendingRun(wikiId, { ...pending, conversationTitle: updated.title });
          }
        }
      }
      setRenameTarget(null);
    } catch (cause) {
      setRenameError(cause instanceof Error ? cause.message : "修改对话名称失败，请重试。");
    } finally {
      renamingRef.current = false;
      setRenaming(false);
    }
  }

  function readPendingRuns(scope: string): PendingQuestion[] {
    return withQuestionStorage(
      () => parsePendingQuestions(window.localStorage.getItem(pendingRunKey(scope))),
      setStorageError
    ) ?? [];
  }

  function savePendingRun(scope: string, pending: PendingQuestion) {
    withQuestionStorage(() => {
      const items = parsePendingQuestions(window.localStorage.getItem(pendingRunKey(scope)))
        .filter((item) => item.requestId !== pending.requestId);
      window.localStorage.setItem(pendingRunKey(scope), JSON.stringify([...items, pending]));
    }, setStorageError);
  }

  function removePendingRun(scope: string, requestId: string) {
    withQuestionStorage(() => {
      const items = parsePendingQuestions(window.localStorage.getItem(pendingRunKey(scope)))
        .filter((item) => item.requestId !== requestId);
      if (items.length) window.localStorage.setItem(pendingRunKey(scope), JSON.stringify(items));
      else window.localStorage.removeItem(pendingRunKey(scope));
    }, setStorageError);
  }

  useEffect(() => {
    const scope = getActiveWikiId();
    setWikiId(scope);
    if (scope) {
      void loadHistory(scope);
    } else {
      setHistoryLoading(false);
    }
    return () => {
      selectionVersion.current++;
      operationVersion.current++;
    };
  }, []);

  useEffect(() => {
    if (!turns.length) return;
    const container = chatMessagesRef.current;
    container?.scrollTo({ behavior: "smooth", top: container.scrollHeight });
  }, [turns]);

  useEffect(() => {
    if (!deleteTarget || deleting) return;
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") closeDeleteDialog();
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget, deleting]);

  function closeDeleteDialog() {
    if (deletingRef.current) return;
    setDeleteTarget(null);
    setDeleteError(null);
  }

  async function loadHistory(scope: string) {
    const version = ++selectionVersion.current;
    const persisted = readPendingRuns(scope);
    const fallbackId = activeConversationRef.current
      ?? withQuestionStorage(() => window.localStorage.getItem(conversationKey(scope)), setStorageError)
      ?? persisted.at(-1)?.conversationId;
    const fallbackTurns = restorePendingTurns([], persisted.filter((item) =>
      item.conversationId === fallbackId || !item.conversationId
    ));
    let queued: ConversationTurn | undefined = fallbackTurns.at(-1);
    if (fallbackTurns.length) setTurns(fallbackTurns);
    switchingRef.current = true;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const items = await api.listQuestionConversations();
      if (version !== selectionVersion.current) return;
      setConversations(items);
      const pending = readPendingRuns(scope);
      const latestPending = pending.at(-1);
      const savedId = activeConversationRef.current
        ?? withQuestionStorage(() => window.localStorage.getItem(conversationKey(scope)), setStorageError);
      const selectedId = !activeConversationRef.current && latestPending && !latestPending.conversationId
        ? undefined
        : items.find((item) => item.id === savedId)?.id
          ?? latestPending?.conversationId ?? items[0]?.id;
      if (!selectedId) {
        activeConversationRef.current = null;
        setActiveConversationId(null);
        const restored = restorePendingTurns([], pending.filter((item) => !item.conversationId));
        setTurns(restored);
        queued = restored.at(-1);
        withQuestionStorage(() => window.localStorage.removeItem(conversationKey(scope)), setStorageError);
        return;
      }
      const conversation = await api.getQuestionConversation(selectedId);
      if (version !== selectionVersion.current) return;
      activeConversationRef.current = selectedId;
      setActiveConversationId(selectedId);
      withQuestionStorage(() => window.localStorage.setItem(conversationKey(scope), selectedId), setStorageError);
      setConversations((current) => upsertQuestionConversation(
        current, conversation.id, conversation.title, new Date().toISOString()
      ));
      const restored = restorePendingTurns(
        conversationTurns(conversation), pending.filter((item) => item.conversationId === selectedId)
      );
      setTurns(restored);
      queued = restored.filter((turn) => turn.requestId).at(-1);
    } catch (cause) {
      if (version === selectionVersion.current) {
        setHistoryError(cause instanceof Error ? cause.message : "无法加载对话记录。");
      }
    } finally {
      if (version === selectionVersion.current) {
        switchingRef.current = false;
        setHistoryLoading(false);
        if (queued) void recoverRun(queued, scope);
      }
    }
  }

  async function refreshConversationList(version: number) {
    try {
      const items = await api.listQuestionConversations();
      if (version === operationVersion.current) {
        setConversations((current) => {
          const merged = items.map((item) => {
            const local = current.find((entry) => entry.id === item.id);
            const merged = local && Date.parse(local.updated_at) > Date.parse(item.updated_at)
              ? local : item;
            return { ...merged, title: customTitlesRef.current.get(item.id) ?? merged.title };
          });
          return [...current.filter((item) => !items.some((entry) => entry.id === item.id)), ...merged];
        });
      }
    } catch (cause) {
      if (version === operationVersion.current) {
        setHistoryError(cause instanceof Error ? cause.message : "无法更新对话列表。");
      }
    }
  }

  function updateTurn(id: string, update: Partial<ConversationTurn>) {
    setTurns((current) => current.map((turn) => turn.id === id ? { ...turn, ...update } : turn));
  }

  function beginQuestionOperation(
    turn: ConversationTurn,
    scope: string,
    submit?: () => ReturnType<typeof api.askWiki>
  ) {
    const version = ++operationVersion.current;
    const savedPending = readPendingRuns(scope).find((item) => item.requestId === turn.requestId);
    let pending: PendingQuestion = {
      ...savedPending,
      requestId: turn.requestId ?? turn.runId!,
      question: turn.question,
      turnId: turn.id,
      conversationId: savedPending?.conversationId ?? activeConversationRef.current ?? undefined,
      runId: turn.runId ?? savedPending?.runId,
      createdAt: turn.createdAt ?? savedPending?.createdAt ?? new Date().toISOString()
    };
    savePendingRun(scope, pending);
    const isCurrent = () => version === operationVersion.current;
    const rememberConversation = (id: string, title: string) => {
      activeConversationRef.current = id;
      setActiveConversationId(id);
      withQuestionStorage(() => window.localStorage.setItem(conversationKey(scope), id), setStorageError);
      setConversations((current) => upsertQuestionConversation(
        current, id, customTitlesRef.current.get(id) ?? title, new Date().toISOString()
      ));
    };
    const operation = createQuestionOperation({
      runId: pending.runId,
      submit,
      isCurrent,
      lookup: (runId, signal) => runId
        ? api.getQuestionRun(runId, signal)
        : api.getQuestionRequest(pending.requestId, signal),
      cancel: (runId) => api.cancelQuestionRun(runId, AbortSignal.timeout(10000)),
      onBusy: (busy) => {
        if (!isCurrent()) return;
        loadingRef.current = busy;
        postingRef.current = busy && !!submit;
        setLoading(busy);
        updateTurn(turn.id, { loading: busy });
      },
      onError: (message) => {
        pending = { ...pending, error: message };
        savePendingRun(scope, pending);
        updateTurn(turn.id, { error: message });
      },
      onCancelling: (cancelling) => updateTurn(turn.id, { cancelling }),
      onCancelError: (cancelError) => {
        pending = { ...pending, cancelError };
        savePendingRun(scope, pending);
        updateTurn(turn.id, { cancelError });
      },
      onRun: (run) => {
        rememberConversation(run.conversation_id, run.conversation_title);
        const cancelled = cancelledQuestion(run.status);
        const completed = completedQuestion(run.status);
        const error = cancelled ? "已停止，此运行无法恢复。"
          : failedQuestion(run.status) ? run.error || "问答运行失败，可检查状态或恢复运行。"
          : undefined;
        pending = {
          ...pending,
          conversationId: run.conversation_id,
          conversationTitle: customTitlesRef.current.get(run.conversation_id) ?? run.conversation_title,
          runId: run.id,
          status: run.status,
          stage: run.stage,
          resumable: !cancelled && !completed && run.resumable,
          error,
          ...(completed || cancelled ? { cancelError: undefined } : {})
        };
        updateTurn(turn.id, {
          runId: run.id,
          status: run.status,
          stage: run.stage,
          resumable: pending.resumable,
          answer: run.answer ?? undefined,
          error,
          ...(completed || cancelled ? { cancelError: undefined } : {})
        });
        if (completed) {
          removePendingRun(scope, pending.requestId);
          if (!run.answer || turn.restored) {
            void api.getQuestionConversation(run.conversation_id).then((conversation) => {
              if (!isCurrent()) return;
              if (turn.restored) {
                setTurns(restorePendingTurns(conversationTurns(conversation), readPendingRuns(scope)
                  .filter((item) => item.conversationId === run.conversation_id)));
                return;
              }
              const last = conversationTurns(conversation).at(-1);
              updateTurn(turn.id, last?.answer || last?.text
                ? { answer: last.answer, text: last.text, error: undefined }
                : { error: "运行已完成，但未找到回答，请重新加载对话。" });
            }).catch((cause: unknown) => {
              if (isCurrent() && !run.answer) updateTurn(turn.id, {
                error: cause instanceof Error ? cause.message : "无法加载已完成的回答。"
              });
            });
          }
        } else {
          savePendingRun(scope, pending);
        }
      },
      onSubmission: (response) => {
        rememberConversation(response.conversation_id, response.conversation_title);
        updateTurn(turn.id, {
          answer: response.answer ? response : undefined,
          text: response.clarification_question && !response.answer
            ? response.clarification_question : undefined,
          runId: response.run_id,
          status: "completed",
          stage: "completed",
          resumable: false,
          error: undefined,
          cancelError: undefined
        });
        removePendingRun(scope, pending.requestId);
        void refreshConversationList(version);
      }
    });
    questionOperationRef.current = { turnId: turn.id, operation };
    updateTurn(turn.id, { error: undefined });
    void operation.start();
    return operation;
  }

  async function sendQuestion(turnId: string, question: string, requestId: string, retry = true) {
    const scope = wikiId;
    if (!scope || loadingRef.current || switchingRef.current || deletingRef.current) return;
    const turn: ConversationTurn = { id: turnId, question, requestId, loading: true };
    // Save the idempotency key before even the recovery lookup or POST is started.
    const pending = readPendingRuns(scope).find((item) => item.requestId === requestId);
    savePendingRun(scope, pending ?? {
      requestId, question, turnId, conversationId: activeConversationRef.current ?? undefined
    });
    if (retry) {
      const version = ++operationVersion.current;
      loadingRef.current = true;
      setLoading(true);
      updateTurn(turnId, { loading: true, error: undefined });
      try {
        const run = await api.getQuestionRequest(requestId, AbortSignal.timeout(10000));
        if (version !== operationVersion.current) return;
        if (run) {
          beginQuestionOperation({ ...turn, runId: run.id }, scope);
          return;
        }
      } catch (cause) {
        if (version === operationVersion.current) {
          const message = cause instanceof Error ? cause.message : "无法检查原提交，请重试。";
          savePendingRun(scope, {
            ...(pending ?? { requestId, question, turnId }),
            error: message
          });
          updateTurn(turnId, {
            loading: false,
            error: message
          });
          loadingRef.current = false;
          setLoading(false);
        }
        return;
      }
    }
    beginQuestionOperation(turn, scope, () => api.askWiki(
      question, pending?.conversationId ?? activeConversationRef.current ?? undefined, requestId
    ));
  }

  function recoverRun(turn: ConversationTurn, scope = wikiId) {
    if ((!turn.runId && !turn.requestId) || !scope || loadingRef.current || switchingRef.current || deletingRef.current) return;
    beginQuestionOperation(turn, scope);
  }

  function resumeRun(turn: ConversationTurn) {
    if (!turn.runId || !turn.resumable || cancelledQuestion(turn.status ?? "") || !wikiId
      || loadingRef.current || switchingRef.current || deletingRef.current) return;
    beginQuestionOperation(turn, wikiId, () => api.resumeQuestionRun(turn.runId!));
  }

  async function cancelRun(turn: ConversationTurn) {
    if (!wikiId || !turn.runId || turn.cancelling || cancelledQuestion(turn.status ?? "")
      || completedQuestion(turn.status ?? "")) return;
    let operation = questionOperationRef.current;
    if (operation?.turnId !== turn.id) {
      if (loadingRef.current) return;
      operation = { turnId: turn.id, operation: beginQuestionOperation(turn, wikiId) };
    }
    await operation.operation.cancel();
  }

  async function selectConversation(id: string) {
    if (postingRef.current || switchingRef.current || deletingRef.current || renamingRef.current || id === activeConversationRef.current) return;
    const version = ++selectionVersion.current;
    let queued: ConversationTurn | undefined;
    switchingRef.current = true;
    setDeleteError(null);
    ++operationVersion.current;
    loadingRef.current = false;
    setLoading(false);
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const conversation = await api.getQuestionConversation(id);
      if (version !== selectionVersion.current) return;
      activeConversationRef.current = id;
      setActiveConversationId(id);
      if (wikiId) withQuestionStorage(() => window.localStorage.setItem(conversationKey(wikiId), id), setStorageError);
      const pending = wikiId ? readPendingRuns(wikiId) : [];
      const restored = restorePendingTurns(
        conversationTurns(conversation), pending.filter((item) => item.conversationId === id)
      );
      setTurns(restored);
      queued = restored.filter((turn) => turn.requestId).at(-1);
    } catch (cause) {
      if (version === selectionVersion.current) {
        setHistoryError(cause instanceof Error ? cause.message : "无法打开对话。");
      }
    } finally {
      if (version === selectionVersion.current) {
        switchingRef.current = false;
        setHistoryLoading(false);
        if (queued && wikiId) void recoverRun(queued, wikiId);
      }
    }
  }

  function submitQuery(question: string) {
    const normalizedQuestion = question.trim();
    if (!normalizedQuestion || loadingRef.current || switchingRef.current || deletingRef.current || historyError || !questionsEnabled || !wikiId) return;

    const id = newRequestId();
    const requestId = newRequestId();
    const nextTurn: ConversationTurn = {
      id,
      question: normalizedQuestion,
      loading: true,
      requestId,
      createdAt: new Date().toISOString()
    };
    setTurns((current) => [...current, nextTurn]);
    setInput("");
    setCopyError(null);
    void sendQuestion(id, normalizedQuestion, requestId, false);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void submitQuery(input);
  }

  function handleInputKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submitQuery(input);
    }
  }

  async function copyAnswer(turnId: string, content: string) {
    setCopyError(null);
    try {
      await navigator.clipboard.writeText(content);
      setCopiedTurnId(turnId);
      window.setTimeout(() => setCopiedTurnId(null), 1600);
    } catch {
      setCopyError("复制失败，请手动选择回答内容。");
    }
  }

  async function startNewConversation() {
    if (!wikiId || postingRef.current || switchingRef.current || deletingRef.current || renamingRef.current || historyError) return;
    const version = ++selectionVersion.current;
    switchingRef.current = true;
    setCreating(true);
    setCreateError(null);
    try {
      const conversation = await api.createQuestionConversation();
      if (version !== selectionVersion.current) return;
      ++operationVersion.current;
      loadingRef.current = false;
      setLoading(false);
      activeConversationRef.current = conversation.id;
      setActiveConversationId(conversation.id);
      setConversations((current) => [conversation, ...current]);
      withQuestionStorage(() => window.localStorage.setItem(conversationKey(wikiId), conversation.id), setStorageError);
      setTurns([]);
      setInput("");
      setCopyError(null);
      setDeleteError(null);
      chatMessagesRef.current?.scrollTo({ top: 0 });
    } catch (cause) {
      if (version === selectionVersion.current) {
        setCreateError(cause instanceof Error ? cause.message : "无法创建对话，请重试。");
      }
    } finally {
      if (version === selectionVersion.current) {
        switchingRef.current = false;
        setCreating(false);
      }
    }
  }

  async function deleteConversation() {
    const target = deleteTarget;
    if (!target || !wikiId || postingRef.current || switchingRef.current || deletingRef.current || loadingRef.current) return;
    const scope = wikiId;
    let reloadSelection = false;
    deletingRef.current = true;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteQuestionConversation(target.id);
      setConversations((current) => current.filter((item) => item.id !== target.id));
      setDeleteTarget(null);
      for (const pending of readPendingRuns(scope)) {
        if (pending.conversationId === target.id) removePendingRun(scope, pending.requestId);
      }
      if (activeConversationRef.current === target.id) {
        ++selectionVersion.current;
        ++operationVersion.current;
        activeConversationRef.current = null;
        setActiveConversationId(null);
        setTurns([]);
        setInput("");
        setCopyError(null);
        withQuestionStorage(() => window.localStorage.removeItem(conversationKey(scope)), setStorageError);
        reloadSelection = true;
      }
    } catch (cause) {
      setDeleteError(cause instanceof Error ? cause.message : "无法删除对话，请重试。");
    } finally {
      deletingRef.current = false;
      setDeleting(false);
    }
    if (reloadSelection) void loadHistory(scope);
  }

  return (
    <div className="question-page">
      <RequestState
        error={featureError}
        loading={featuresLoading}
        onRetry={reloadFeatures}
      />
      {storageError ? <div className="chat-error" role="alert">{storageError}</div> : null}
      <section className="chat-workspace" aria-label="知识问答对话">
        <div className="chat-body">
          <nav aria-label="历史对话" className="chat-history">
            <div className="chat-history-head">
              <strong>历史对话</strong>
              <button
                aria-label={creating ? "正在创建新对话" : "新对话"}
                className="chat-new-conversation"
                disabled={!wikiId || deleting || creating || postingRef.current || historyLoading || !!historyError}
                onClick={() => void startNewConversation()}
                title="新对话"
                type="button"
              >
                <span aria-hidden="true">{creating ? "…" : "+"}</span>
              </button>
            </div>
            {historyError ? (
              <div className="chat-error" role="alert">
                {historyError}
                <button
                  disabled={loading || historyLoading}
                  onClick={() => wikiId && void loadHistory(wikiId)}
                  type="button"
                >
                  重试
                </button>
              </div>
            ) : null}
            {createError ? <div className="chat-error" role="alert">{createError}</div> : null}
            {historyLoading ? <span role="status">正在加载对话…</span> : null}
            {!historyLoading && !conversations.length ? <span>暂无历史对话</span> : null}
            {conversations.map((conversation) => (
              <div
                className="chat-history-item"
                key={conversation.id}
              >
                <button
                  aria-current={activeConversationId === conversation.id ? "true" : undefined}
                  className="chat-history-select"
                  disabled={deleting || postingRef.current || historyLoading || renaming}
                  onClick={() => void selectConversation(conversation.id)}
                  title={conversation.title}
                  type="button"
                >
                  <span className="chat-history-title">{conversation.title || "未命名对话"}</span>
                  <time dateTime={conversation.updated_at}>{formatDateTime(conversation.updated_at)}</time>
                </button>
                <button
                  aria-label={`删除对话：${conversation.title || "未命名对话"}`}
                  className="chat-history-delete"
                  disabled={deleting || loading || creating || historyLoading}
                  onClick={() => {
                    setDeleteError(null);
                    setDeleteTarget(conversation);
                  }}
                  title="删除对话"
                  type="button"
                >
                  <svg aria-hidden="true" fill="none" height="16" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" viewBox="0 0 24 24" width="16">
                    <path d="M4 7h16M10 4h4m-8 3 1 13h10l1-13M10 11v6m4-6v6" />
                  </svg>
                </button>
              </div>
            ))}
          </nav>
          <div className="chat-main">
            <header className="chat-conversation-head">
              {renameTarget ? (
                <input
                  aria-label="对话名称"
                  autoFocus
                  className="chat-conversation-title-input"
                  disabled={renaming}
                  maxLength={120}
                  onBlur={() => void saveConversationTitle()}
                  onChange={(event) => setRenameTitle(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      void saveConversationTitle();
                    } else if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      cancelTitleEdit();
                    }
                  }}
                  value={renameTitle}
                />
              ) : activeConversationId ? (
                <button
                  aria-label="修改当前对话名称"
                  className="chat-conversation-title"
                  disabled={creating || historyLoading || deleting}
                  onClick={() => {
                    const conversation = conversations.find((item) => item.id === activeConversationId);
                    if (!conversation) return;
                    titleEditCancelledRef.current = false;
                    setRenameTarget(conversation);
                    setRenameTitle(conversation.title);
                    setRenameError(null);
                  }}
                  title="点击修改对话名称"
                  type="button"
                >
                  {conversations.find((item) => item.id === activeConversationId)?.title || "新对话"}
                </button>
              ) : (
                <strong className="chat-conversation-title">新对话</strong>
              )}
              {renaming ? <span role="status">正在保存…</span> : null}
              {renameError ? <div className="chat-error" role="alert">{renameError}</div> : null}
            </header>
        <div className="chat-messages" aria-live="polite" ref={chatMessagesRef}>
          {turns.length === 0 ? (
            <div className="chat-welcome">
              <h2>有什么想从知识库中了解的？</h2>
              <p>我会优先使用 Wiki 中已经整理的知识，必要时追溯原始资料，并明确提示证据不足。</p>
              <div className="suggested-questions">
                {SUGGESTED_QUESTIONS.map((question) => (
                  <button
                    disabled={!questionsEnabled || loading || deleting || historyLoading || !!historyError}
                    key={question}
                    onClick={() => void submitQuery(question)}
                    type="button"
                  >
                    {question}
                    <span aria-hidden="true">→</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((turn) => (
              <div className="chat-turn" key={turn.id}>
                {turn.question ? (
                  <div className="chat-message chat-message-user">
                    <div className="chat-bubble chat-bubble-user">{turn.question}</div>
                    <div className="chat-avatar chat-avatar-user" aria-hidden="true">我</div>
                  </div>
                ) : null}
                <div className="chat-message chat-message-assistant">
                  <div className="chat-avatar chat-avatar-assistant" aria-hidden="true">AI</div>
                  <div className="chat-bubble chat-bubble-assistant">
                    {turn.loading ? (
                      <div className="assistant-thinking" role="status">
                        <span className="loading-dot" />
                        {turn.status
                          ? `${["queued", "pending"].includes(turn.status.toLowerCase()) ? "排队中" : "运行中"}：${turn.stage || turn.status}`
                          : "正在查阅知识库…"}
                      </div>
                    ) : null}
                    {turn.status && !turn.loading && !turn.error && !turn.answer && !turn.text ? (
                      <div className="assistant-thinking" role="status">
                        {turn.stage || turn.status}
                        {turn.runId || turn.requestId ? (
                          <button disabled={loading} onClick={() => void recoverRun(turn)} type="button">
                            检查运行状态
                          </button>
                        ) : null}
                      </div>
                    ) : null}
                    {turn.error ? (
                      <div className="chat-error" role="alert">
                        {turn.error}
                        {turn.runId ? (
                          <>
                            <button disabled={loading} onClick={() => void recoverRun(turn)} type="button">
                              检查运行状态
                            </button>
                            {turn.resumable && !cancelledQuestion(turn.status ?? "") ? (
                              <button disabled={loading} onClick={() => void resumeRun(turn)} type="button">
                                恢复运行
                              </button>
                            ) : null}
                            {["failed", "error", "cancelled", "canceled"].includes(turn.status?.toLowerCase() ?? "") ? (
                              <button disabled={loading} onClick={() => void submitQuery(turn.question)} type="button">
                                重新提问
                              </button>
                            ) : null}
                          </>
                        ) : turn.requestId ? (
                          <button
                            disabled={loading}
                            onClick={() => void sendQuestion(turn.id, turn.question, turn.requestId!)}
                            type="button"
                          >
                            恢复提交（沿用原请求）
                          </button>
                        ) : null}
                      </div>
                    ) : null}
                    {turn.resumable && !turn.loading && !turn.error
                      && !cancelledQuestion(turn.status ?? "") ? (
                      <button disabled={loading} onClick={() => void resumeRun(turn)} type="button">
                        恢复运行
                      </button>
                    ) : null}
                    {(turn.loading || turn.runId) && !completedQuestion(turn.status ?? "")
                      && !cancelledQuestion(turn.status ?? "") ? (
                      <button
                        disabled={!turn.runId || turn.cancelling || (loading && !turn.loading)}
                        onClick={() => void cancelRun(turn)}
                        title="停止后无法恢复此运行"
                        type="button"
                      >
                        {turn.cancelling ? "正在停止…" : turn.runId ? "停止运行" : "等待运行登记…"}
                      </button>
                    ) : null}
                    {turn.cancelError ? (
                      <div className="chat-error" role="alert">停止请求失败：{turn.cancelError}。运行尚未确认停止，请重试。</div>
                    ) : null}
                    {turn.answer ? (
                      <AnswerMessage
                        answer={turn.answer}
                        copied={copiedTurnId === turn.id}
                        onCopy={() => void copyAnswer(turn.id, turn.answer?.answer ?? "")}
                      />
                    ) : null}
                    {!turn.answer && turn.text ? (
                      <>
                        <div className="assistant-answer">{turn.text}</div>
                        {turn.citations?.length ? (
                          <div className="assistant-message-actions">
                            <span>{turn.citations.length} 条引用</span>
                          </div>
                        ) : null}
                      </>
                    ) : null}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>

        <footer className="chat-composer-area">
          {copyError ? <div className="chat-copy-error" role="alert">{copyError}</div> : null}
          <form className="chat-composer" onSubmit={submit}>
            <textarea
              aria-label="向知识库提问"
              disabled={!questionsEnabled || loading || deleting || historyLoading || !!historyError}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder={
                questionsEnabled
                  ? loading
                    ? "正在生成回答…"
                    : "向知识库提问"
                  : "知识问答当前不可用"
              }
              rows={2}
              value={input}
            />
            <div className="chat-composer-toolbar">
              <button
                aria-label="发送问题"
                className="chat-send-button"
                disabled={!input.trim() || loading || deleting || historyLoading || !!historyError || !questionsEnabled}
                type="submit"
              >
                ↑
              </button>
            </div>
          </form>
          <p className="chat-disclaimer">Enter 发送，Shift + Enter 换行。回答可能存在遗漏，请结合引用来源判断。</p>
        </footer>
          </div>
        </div>
      </section>
      {deleteTarget ? (
        <div
          className="settings-modal-backdrop"
          onMouseDown={closeDeleteDialog}
          role="presentation"
        >
          <div
            aria-describedby="delete-conversation-description"
            aria-labelledby="delete-conversation-title"
            aria-modal="true"
            className="settings-modal settings-modal-small chat-delete-dialog"
            onMouseDown={(event) => event.stopPropagation()}
            role="dialog"
          >
            <div aria-hidden="true" className="danger-dialog-icon">!</div>
            <h2 id="delete-conversation-title">删除对话？</h2>
            <p id="delete-conversation-description">
              “{deleteTarget.title || "未命名对话"}”中的全部问答记录将永久删除，此操作无法撤销。
            </p>
            {deleteError ? <div className="chat-error" role="alert">{deleteError}</div> : null}
            <div className="settings-modal-actions">
              <button autoFocus className="button button-secondary" disabled={deleting} onClick={closeDeleteDialog} type="button">取消</button>
              <button className="button button-danger" disabled={deleting} onClick={() => void deleteConversation()} type="button">
                {deleting ? "正在删除…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function AnswerMessage({
  answer,
  copied,
  onCopy
}: {
  answer: WikiQuestionResponse;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <>
      <div className="assistant-answer">{answer.answer}</div>
      {answer.uncertainty ? (
        <div className="answer-uncertainty">{answer.uncertainty}</div>
      ) : null}
      {answer.degradations.map((degradation) => (
        <div className="answer-uncertainty" key={degradation.channel}>
          {degradation.reason}
        </div>
      ))}
      {answer.evidence.length ? (
        <div className="answer-evidence">
          <strong>参考来源</strong>
          <div className="evidence-list">
            {answer.evidence.map((evidence, index) => (
              <article className="evidence-card" key={`${evidence.document_id}-${index}`}>
                <span>{index + 1}</span>
                <div>
                  <strong>
                    {typeof evidence.metadata.title === "string"
                      ? evidence.metadata.title
                      : evidence.document_id}
                  </strong>
                  <small>{evidence.channels.join(" · ") || "知识库"}</small>
                </div>
              </article>
            ))}
          </div>
        </div>
      ) : null}
      <div className="assistant-message-actions">
        <StatusBadge tone={answer.insufficient_evidence ? "warning" : "positive"}>
          {answer.insufficient_evidence ? "证据不足" : "已有证据支持"}
        </StatusBadge>
        <span>
          {answer.layer === "hybrid"
            ? "Wiki + Raw 融合"
            : answer.layer === "raw"
              ? "Raw 兜底"
              : "Wiki"}
        </span>
        {answer.reranker_used ? <span>已重排</span> : null}
        <span>{answer.citations.length} 条引用</span>
        <button onClick={onCopy} type="button">{copied ? "已复制" : "复制回答"}</button>
      </div>
    </>
  );
}
