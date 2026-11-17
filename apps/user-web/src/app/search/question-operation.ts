import type { WikiQuestionResponse, WikiQuestionRun, WikiQuestionSubmission } from "@wiki-agent/web-ui";

export type ConversationTurn = {
  id: string;
  question: string;
  loading: boolean;
  answer?: WikiQuestionResponse;
  text?: string;
  citations?: string[];
  error?: string;
  runId?: string;
  requestId?: string;
  status?: string;
  stage?: string | null;
  resumable?: boolean;
  cancelling?: boolean;
  cancelError?: string;
  restored?: boolean;
  createdAt?: string;
};

export type PendingQuestion = {
  requestId: string;
  question: string;
  turnId: string;
  conversationId?: string;
  runId?: string;
  status?: string;
  stage?: string | null;
  error?: string;
  cancelError?: string;
  resumable?: boolean;
  conversationTitle?: string;
  createdAt?: string;
};

export const completedQuestion = (status: string) =>
  ["completed", "complete", "succeeded", "success", "done"].includes(status.toLowerCase());
export const cancelledQuestion = (status: string) =>
  ["cancelled", "canceled"].includes(status.toLowerCase());
export const failedQuestion = (status: string) =>
  ["failed", "error"].includes(status.toLowerCase());

export function withQuestionStorage<T>(
  action: () => T,
  onFailure: (message: string) => void
): T | undefined {
  try {
    return action();
  } catch {
    onFailure("无法保存或读取本地问答状态。刷新后可能无法恢复未完成的提问；请保留此页面并检查浏览器存储设置。");
    return undefined;
  }
}

export function restorePendingTurns(turns: ConversationTurn[], pending: PendingQuestion[]) {
  const restored = [...turns];
  for (const item of pending) {
    const last = restored.at(-1);
    const turn: ConversationTurn = {
      id: item.turnId,
      question: item.question,
      loading: false,
      requestId: item.requestId,
      runId: item.runId,
      status: item.status,
      stage: item.stage,
      resumable: !cancelledQuestion(item.status ?? "") && item.resumable,
      error: cancelledQuestion(item.status ?? "")
        ? "已停止，此运行无法恢复。"
        : item.error,
      cancelError: item.cancelError,
      restored: true,
      createdAt: item.createdAt
    };
    if (last?.question === item.question && !last.answer && !last.text && !last.requestId) {
      restored[restored.length - 1] = turn;
    } else {
      restored.push(turn);
    }
  }
  return restored.sort((a, b) => a.createdAt && b.createdAt
    ? Date.parse(a.createdAt) - Date.parse(b.createdAt) : 0);
}

export function parsePendingQuestions(value: string | null): PendingQuestion[] {
  if (!value) return [];
  const parsed: unknown = JSON.parse(value);
  return (Array.isArray(parsed) ? parsed : [parsed]).flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const pending = entry as PendingQuestion;
    if (typeof pending.question !== "string" || typeof pending.turnId !== "string") return [];
    // The old persistence format only recorded a run ID after the POST returned.
    const requestId = pending.requestId || pending.runId;
    if (typeof requestId !== "string" || !requestId) return [];
    return [{ ...pending, requestId }];
  });
}

export function upsertQuestionConversation<T extends {
  id: string; title: string; created_at: string; updated_at: string;
}>(items: T[], id: string, title: string, now: string): T[] {
  const existing = items.find((item) => item.id === id);
  const updated = existing
    ? { ...existing, title: title || existing.title, updated_at: now }
    : { id, title, created_at: now, updated_at: now } as T;
  return [updated, ...items.filter((item) => item.id !== id)];
}

type QuestionOperationOptions = {
  runId?: string;
  lookup: (runId: string | undefined, signal: AbortSignal) => Promise<WikiQuestionRun | null>;
  submit?: () => Promise<WikiQuestionSubmission>;
  cancel: (runId: string) => Promise<WikiQuestionRun>;
  isCurrent: () => boolean;
  onRun: (run: WikiQuestionRun) => void;
  onSubmission: (response: WikiQuestionSubmission) => void;
  onError: (message: string) => void;
  onCancelError: (message: string | undefined) => void;
  onCancelling: (value: boolean) => void;
  onBusy: (value: boolean) => void;
  attempts?: number;
  delay?: () => Promise<void>;
};

export function createQuestionOperation(options: QuestionOperationOptions) {
  let runId = options.runId;
  let outcome: "completed" | "cancelled" | undefined;
  let postPending = !!options.submit;
  let cancelling = false;
  let pollBusy = true;
  let resumableObservations = 0;
  const current = () => options.isCurrent() && !outcome;
  const finish = () => {
    pollBusy = false;
    options.onBusy(outcome ? false : cancelling);
  };
  const applyRun = (run: WikiQuestionRun) => {
    if (!current()) return;
    runId = run.id;
    if (completedQuestion(run.status)) outcome = "completed";
    else if (cancelledQuestion(run.status)) outcome = "cancelled";
    options.onRun(run);
    if (outcome) finish();
  };

  async function poll() {
    const attempts = options.attempts ?? 60;
    for (let attempt = 0; attempt < attempts && current(); attempt++) {
      try {
        // A hung lookup must not turn a bounded recovery into an unbounded wait.
        const run = await options.lookup(runId, AbortSignal.timeout(10000));
        if (!current()) return;
        if (run) {
          applyRun(run);
          if (outcome) return;
          if (failedQuestion(run.status) || run.resumable) {
            resumableObservations++;
            // A resume can briefly expose its old checkpoint, but a hung POST
            // must not block recovery of a server-confirmed failure indefinitely.
            if (!postPending || resumableObservations >= 2) {
              finish();
              return;
            }
          } else resumableObservations = 0;
        }
      } catch (cause) {
        if (current()) {
          options.onError(cause instanceof Error ? cause.message : "无法获取运行状态，请重试。");
          finish();
        }
        return;
      }
      if (attempt + 1 < attempts) {
        await (options.delay?.() ?? new Promise((resolve) => setTimeout(resolve, 2000)));
      }
    }
    if (current()) {
      options.onError("状态检查已暂停，稍后可手动检查运行状态或恢复提交。");
      finish();
    }
  }

  return {
    async start() {
      options.onBusy(true);
      // The synchronous POST and status lookup intentionally run independently.
      if (options.submit) {
        void (async () => {
          try {
            const response = await options.submit!();
            postPending = false;
            if (!current()) return;
            outcome = "completed";
            options.onSubmission(response);
            finish();
          } catch (cause) {
            postPending = false;
            if (current()) {
              options.onError(cause instanceof Error ? cause.message : "提交失败，请检查运行状态。");
              // Polling still discovers work persisted before a lost POST response.
            }
          }
        })();
      }
      await poll();
    },
    async cancel() {
      if (!runId || !current() || cancelling) return;
      cancelling = true;
      options.onBusy(true);
      options.onCancelling(true);
      options.onCancelError(undefined);
      try {
        applyRun(await options.cancel(runId));
      } catch (cause) {
        if (current()) {
          options.onCancelError(cause instanceof Error ? cause.message : "停止失败，请重试。");
        }
      } finally {
        cancelling = false;
        if (options.isCurrent()) {
          options.onCancelling(false);
          options.onBusy(pollBusy);
        }
      }
    }
  };
}
