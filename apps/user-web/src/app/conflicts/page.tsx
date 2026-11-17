"use client";

import {
  ApiError,
  EmptyState,
  RequestState,
  Section,
  StatusBadge,
  api,
  formatDateTime,
  formatLabel,
  type Draft,
  useApiResource
} from "@wiki-agent/web-ui";
import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { sourceRunRevisionIds } from "../wiki/source-processing";
import { draftCompilationRun, groupConflicts, openDrafts, reviewExplanationError, reviewFailureMessage, reviewReasons } from "./review";

export default function ConflictsPage() {
  const load = useCallback(async () => {
    const [drafts, runs, conflicts] = await Promise.all([
      api.listDrafts(), api.listRuns(), api.listConflicts()
    ]);
    return { drafts, runs, conflicts };
  }, []);
  const { data, error, loading, reload, setData } = useApiResource(load);
  const [reviewing, setReviewing] = useState<Draft | null>(null);
  const [decision, setDecision] = useState<"publish" | "reject">("publish");
  const [explanation, setExplanation] = useState("");
  const [saving, setSaving] = useState(false);
  const reviewInFlight = useRef(false);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ message: string; error: boolean } | null>(null);

  useEffect(() => {
    if (!reviewing || saving) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setReviewing(null);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [reviewing, saving]);

  function openReview(draft: Draft) {
    setReviewing(draft);
    setDecision("publish");
    setExplanation("");
    setReviewError(null);
    setNotice(null);
  }

  async function submitReview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!reviewing || reviewInFlight.current) return;
    const reason = explanation.trim();
    const validationError = reviewExplanationError(decision, reason);
    if (validationError) {
      setReviewError(validationError);
      return;
    }
    reviewInFlight.current = true;
    setSaving(true);
    setReviewError(null);
    try {
      if (decision === "publish") {
        const updated = await api.publishDraft(reviewing.id, reason);
        setData((current) => current ? {
          ...current,
          drafts: current.drafts.map((draft) => draft.id === updated.id ? updated : draft)
        } : current);
      } else {
        await api.rejectDraft(reviewing.id, reason);
        setData((current) => current ? {
          ...current,
          drafts: current.drafts.filter((draft) => draft.id !== reviewing.id)
        } : current);
      }
      setReviewing(null);
      setNotice({
        message: decision === "publish" ? "草稿已发布到当前 Wiki。" : "已拒绝发布，草稿、对应原件及处理记录已删除。",
        error: false
      });
      reload();
    } catch (cause) {
      if (cause instanceof ApiError && decision === "reject" &&
          cause.detail === "Source deleted, but object cleanup failed") {
        setData((current) => current ? {
          ...current,
          drafts: current.drafts.filter((draft) => draft.id !== reviewing.id)
        } : current);
        setReviewing(null);
        setNotice({ message: reviewFailureMessage(cause.message), error: true });
      } else if (cause instanceof ApiError && cause.status === 503 &&
          cause.detail && typeof cause.detail === "object" &&
          "commit" in cause.detail) {
        setReviewing(null);
        setNotice({
          message: "草稿已发布，但搜索索引构建失败。请在模型设置中重建 Wiki 索引。",
          error: true
        });
      } else {
        setReviewError(cause instanceof Error
          ? reviewFailureMessage(cause.message)
          : "审核操作失败，请稍后重试。");
      }
      reload();
    } finally {
      reviewInFlight.current = false;
      setSaving(false);
    }
  }

  const pending = openDrafts(data?.drafts ?? []);
  const conflicts = groupConflicts(data?.conflicts ?? []);
  const reviewingRun = reviewing ? draftCompilationRun(reviewing, data?.runs ?? []) : undefined;
  const reviewingSourceCount = reviewingRun ? sourceRunRevisionIds(reviewingRun).length : 0;
  const reviewingBatch = reviewingRun?.input_json.batch === true || reviewingSourceCount > 1;
  return (
    <>
      {notice ? (
        <div className={notice.error ? "review-notice review-notice-error" : "review-notice"}
          role={notice.error ? "alert" : "status"}>
          <span>{notice.message}</span>
          <button aria-label="关闭提示" onClick={() => setNotice(null)} type="button">×</button>
        </div>
      ) : null}
      <RequestState error={error} loading={loading && !data} onRetry={reload} />
      {!error && data ? (
        <>
          <Section
            subtitle="只显示你在当前 Wiki 中尚未决定的草稿；发布或拒绝前请先查看变更内容。"
            title={`我的待审核草稿${pending.length ? ` · ${pending.length}` : ""}`}
          >
            {pending.length === 0 ? (
              <EmptyState title="暂无待审核草稿" description="需要人工确认的编译结果会出现在这里。" />
            ) : (
              <div className="review-list">
                {pending.map((draft) => {
                  const reasons = reviewReasons(draft, data.runs);
                  const run = draftCompilationRun(draft, data.runs);
                  const sourceCount = run ? sourceRunRevisionIds(run).length : 0;
                  return (
                    <article className="review-card" key={draft.id}>
                      <div className="review-card-head">
                        <div>
                          <strong>{draft.title}</strong>
                          <small>
                            创建于 {formatDateTime(draft.created_at)} · {Object.keys(draft.files).length} 个变更文件
                            {sourceCount ? ` · ${sourceCount} 份来源${run?.input_json.batch === true || sourceCount > 1 ? "（整批统一审核）" : ""}` : ""}
                          </small>
                        </div>
                        <StatusBadge tone="warning">待审核</StatusBadge>
                      </div>
                      <p>{reasons.length ? reasons.join(" · ") : "此草稿需要你确认后才能发布。"}</p>
                      <button className="button button-secondary" onClick={() => openReview(draft)} type="button">
                        查看并审核
                      </button>
                    </article>
                  );
                })}
              </div>
            )}
          </Section>
          <Section subtitle="已发布到当前 Wiki 的 pending/ 页面；草稿中的冲突请在上方先审核。" title="已发布的待处理冲突">
            {conflicts.length === 0 ? (
              <EmptyState title="暂无已发布的冲突页面" description="待审核草稿中的冲突不会在这里提前显示。" />
            ) : (
              <div className="review-list">
                {conflicts.map((group) => (
                  <article className="review-card conflict-group" key={group.id}>
                    <div className="review-card-head">
                      <div>
                        <strong>{group.title}</strong>
                        <small>{group.conflicts.length} 个冲突页面 · {group.sources.length} 个相关来源 · 更新于 {formatDateTime(group.updated)}</small>
                      </div>
                      <StatusBadge tone="warning">{formatLabel("pending")}</StatusBadge>
                    </div>
                    {group.sources.length ? (
                      <div className="conflict-group-sources">
                        <span>相关来源</span>
                        <ul>{group.sources.map((path) => <li key={path}>
                          <Link href={`/wiki?path=${encodeURIComponent(path)}`}>{path}</Link>
                        </li>)}</ul>
                      </div>
                    ) : null}
                    <ul className="conflict-group-pages">
                      {group.conflicts.map((conflict) => <li key={conflict.path}>
                        <Link href={`/wiki?path=${encodeURIComponent(conflict.path)}`}>
                          {conflict.title}<small>{conflict.path}</small>
                        </Link>
                      </li>)}
                    </ul>
                  </article>
                ))}
              </div>
            )}
          </Section>
        </>
      ) : null}
      {reviewing ? (
        <div className="settings-modal-backdrop" role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !saving) setReviewing(null);
          }}>
          <section
            aria-labelledby="review-draft-title"
            aria-modal="true"
            className="settings-modal review-dialog"
            role="dialog"
          >
            <div className="settings-modal-head">
              <div>
                <span className="eyebrow">个人 Wiki 草稿</span>
                <h2 id="review-draft-title">审核“{reviewing.title}”</h2>
              </div>
              <button aria-label="关闭审核窗口" disabled={saving}
                onClick={() => setReviewing(null)} type="button">×</button>
            </div>
            <p className="review-intro">
              {reviewingBatch
                ? `此草稿包含同一批次的 ${reviewingSourceCount} 份来源，请检查下方全部变更文件。整批只能统一发布或拒绝；拒绝会删除本批次所有导入原件及处理记录。`
                : "请检查全部变更文件及来源引用。发布会立即更新当前 Wiki；拒绝发布会删除此次草稿、对应原件及处理记录。"}
            </p>
            <div className="review-files">
              {Object.entries(reviewing.files).map(([path, content]) => (
                <details key={path}>
                  <summary>{content === null ? "删除 · " : "更新 · "}{path}</summary>
                  {content === null ? (
                    <p>此变更会删除该 Wiki 页面。</p>
                  ) : (
                    <pre>{content}</pre>
                  )}
                </details>
              ))}
            </div>
            <form className="settings-form review-decision" onSubmit={(event) => void submitReview(event)}>
              {decision === "reject" ? (
                <p className="form-error" role="alert">
                  {reviewingBatch
                    ? `确认后将永久删除整批草稿、本批次全部 ${reviewingSourceCount} 份导入原件及处理记录，无法只拒绝某一份，且无法撤销。`
                    : "确认后将永久删除此次草稿、对应导入原件及处理记录，无法撤销。"}
                  不会删除其他资料或已发布的 Wiki 页面。
                </p>
              ) : null}
              <label className="field">
                {decision === "publish" ? "发布说明（可选）" : "拒绝原因"}
                <textarea disabled={saving}
                  maxLength={decision === "publish" ? 256 : 2000}
                  minLength={decision === "reject" ? 3 : undefined}
                  onChange={(event) => setExplanation(event.target.value)}
                  placeholder={decision === "publish" ? "说明这次知识更新的内容，可留空" : "说明不发布的原因"}
                  required={decision === "reject"} rows={3} value={explanation} />
              </label>
              {reviewError ? <div className="form-error" role="alert">{reviewError}</div> : null}
              <div className="settings-modal-actions">
                <button className="button button-secondary" disabled={saving}
                  onClick={() => setReviewing(null)} type="button">取消审核</button>
                <button className="button button-danger-outline" disabled={saving}
                  onClick={() => {
                    setDecision(decision === "publish" ? "reject" : "publish");
                    setExplanation("");
                    setReviewError(null);
                  }} type="button">
                  {decision === "publish" ? "拒绝发布" : "返回发布"}
                </button>
                <button className={decision === "publish" ? "button" : "button button-danger"}
                  disabled={saving || reviewExplanationError(decision, explanation) !== null} type="submit">
                  {saving ? "正在处理…" : decision === "publish" ? "确认发布" : "确认删除"}
                </button>
              </div>
            </form>
          </section>
        </div>
      ) : null}
    </>
  );
}
