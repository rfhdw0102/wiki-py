"use client";

import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ApiError,
  RequestState,
  StatusBadge,
  api,
  formatDateTime,
  formatLabel,
  type Source,
  type SourceSpan,
  useApiResource
} from "@wiki-agent/web-ui";
import {
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import {
  hasVisibleSourceForRun,
  batchIngestResult,
  importFailureMessage,
  ingestResult,
  isActiveImportRun,
  sourceProcessingStatus,
  canRetrySource,
  sourceIsIdleForCompilation,
  sourceRunRevisionIds,
  type SourceProcessingStatus
} from "../wiki/source-processing";
import { droppedFiles, prepareImportFiles, selectedFiles, type ImportFile } from "../wiki/import-files";
import { WebBrowserImport } from "./web-browser-import";
import { websiteImportPayload } from "./website-import";
import { sourceDeletionErrorMessage } from "./source-deletion";
import { sourceMarkdown } from "./source-preview";
import {
  runSourceBatch,
  selectedSources,
  sourceCompilationPlan,
  sourceSelectionState,
  toggleVisibleSources,
  validateSourceName
} from "./source-management";

type ImportTab = "file" | "url";

function uploadErrorMessage(cause: unknown): string {
  if (cause instanceof ApiError && cause.status === 413) {
    return "文件超过上传大小限制，请选择较小的文件。";
  }
  if (cause instanceof ApiError && cause.status === 415) {
    return "文件格式不受支持，请选择 PDF、DOCX、Markdown、TXT 或 HTML。";
  }
  return cause instanceof Error ? cause.message : "上传失败，请稍后重试。";
}

function compilationErrorMessage(cause: unknown): string {
  return cause instanceof ApiError && (cause.status === 422 || cause.status === 503)
    ? importFailureMessage(cause.message)
    : cause instanceof ApiError && cause.status === 409
      ? "该资料正在处理、等待审核或已经发布，不能再次提交编译。"
      : cause instanceof Error ? cause.message : "重新提交失败，请稍后重试。";
}

function deletionErrorMessage(cause: unknown): string {
  return cause instanceof Error
    ? sourceDeletionErrorMessage(cause.message, cause instanceof ApiError ? cause.status : undefined)
    : "删除失败，请稍后重试。";
}

export function SourceBrowser() {
  const load = useCallback(async () => {
    const [sources, features, runs, drafts] = await Promise.all([
      api.listSources(),
      api.listFeatures(),
      api.listRuns(),
      api.listDrafts()
    ]);
    return { sources, features, runs, drafts };
  }, []);
  const { data, error, loading, reload, setData } = useApiResource(load);
  const [sourceQuery, setSourceQuery] = useState("");
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(null);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const openedRevision = useRef<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [importTab, setImportTab] = useState<ImportTab>("file");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [browserUrl, setBrowserUrl] = useState<string | null>(null);
  const [browserCleanupError, setBrowserCleanupError] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [importProgress, setImportProgress] = useState<string | null>(null);
  const [importError, setImportError] = useState<string | null>(null);
  const [importNotice, setImportNotice] = useState<string | null>(null);
  const [submittedRunIds, setSubmittedRunIds] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const [retryingSourceId, setRetryingSourceId] = useState<string | null>(null);
  const [sourceActionError, setSourceActionError] = useState<{ id: string; message: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Source | null>(null);
  const [deletingSource, setDeletingSource] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [checkedSourceIds, setCheckedSourceIds] = useState<Set<string>>(new Set());
  const selectAllInput = useRef<HTMLInputElement>(null);
  const operation = useRef<"import" | "compile" | "delete" | "rename" | null>(null);
  const [batchAction, setBatchAction] = useState<"compile" | "delete" | null>(null);
  const [batchProgress, setBatchProgress] = useState<string | null>(null);
  const [batchReport, setBatchReport] = useState<{ message: string; failed: boolean } | null>(null);
  const [bulkDeleteTargets, setBulkDeleteTargets] = useState<Source[] | null>(null);
  const [renameTarget, setRenameTarget] = useState<Source | null>(null);
  const [renameName, setRenameName] = useState("");
  const [renamingSource, setRenamingSource] = useState(false);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [renameLocation, setRenameLocation] = useState<"list" | "preview">("list");
  const renameCancelled = useRef(false);
  const busy = importing || Boolean(browserUrl) || Boolean(retryingSourceId) ||
    deletingSource || Boolean(batchAction) || renamingSource;

  const uploadEnabled =
    data?.features.some((flag) => flag.key === "source_upload" && flag.enabled) ?? false;
  const crawlEnabled =
    data?.features.some((flag) => flag.key === "website_crawl" && flag.enabled) ?? false;
  const filteredSources = useMemo(() => {
    const query = sourceQuery.trim().toLocaleLowerCase("zh-CN");
    if (!query) return data?.sources ?? [];
    return (data?.sources ?? []).filter((source) =>
      source.name.toLocaleLowerCase("zh-CN").includes(query)
    );
  }, [data?.sources, sourceQuery]);
  const checkedSources = selectedSources(data?.sources ?? [], checkedSourceIds);
  const visibleSelection = sourceSelectionState(filteredSources, checkedSourceIds);
  const compilationPlan = sourceCompilationPlan(checkedSources, (source) =>
    canRetrySource(sourceProcessingStatus(source, data?.runs ?? [], data?.drafts ?? []), source) &&
    sourceIsIdleForCompilation(source, data?.runs ?? [], data?.drafts ?? [])
  );
  const failedUpload = data?.runs.find(
    (run) => submittedRunIds.includes(run.id) &&
      ["failed", "published_index_failed"].includes(run.status) &&
      !hasVisibleSourceForRun(run, filteredSources)
  );

  useEffect(() => {
    if (selectAllInput.current) selectAllInput.current.indeterminate = visibleSelection.mixed;
  }, [visibleSelection.mixed, data]);

  useEffect(() => {
    if (!data) return;
    const available = new Set(data.sources.map((source) => source.id));
    setCheckedSourceIds((ids) => {
      const retained = new Set([...ids].filter((id) => available.has(id)));
      return retained.size === ids.size ? ids : retained;
    });
  }, [data?.sources]);

  useEffect(() => {
    if (!renameTarget && !bulkDeleteTargets) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape" || operation.current) return;
      renameCancelled.current = true;
      setRenameTarget(null);
      setBulkDeleteTargets(null);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [renameTarget, bulkDeleteTargets]);

  useEffect(() => {
    if (!data) return;
    const revisionId = new URLSearchParams(window.location.search).get("revision");
    const spanId = new URLSearchParams(window.location.search).get("span");
    if (!revisionId || openedRevision.current === revisionId) return;
    let cancelled = false;
    void api.getSourceRevision(revisionId).then((revision) => {
      if (cancelled) return;
      openedRevision.current = revisionId;
      if (data.sources.some((source) => source.id === revision.source_id)) {
        setSelectedSourceId(revision.source_id);
        setSelectedRevisionId(revision.id);
        setSelectedSpanId(spanId);
      } else {
        setSelectionError("引用的原件已不存在，请刷新来源列表。");
      }
    }).catch((cause: unknown) => {
      if (!cancelled) {
        openedRevision.current = revisionId;
        setSelectionError(cause instanceof Error ? cause.message : "无法打开引用的原件。");
      }
    });
    return () => { cancelled = true; };
  }, [data?.sources]);

  useEffect(() => {
    if (!data?.runs.some(isActiveImportRun)) return;
    const timer = window.setInterval(reload, 3000);
    return () => window.clearInterval(timer);
  }, [data?.runs, reload]);

  useEffect(() => {
    if (!data) return;
    const finishedUploads = submittedRunIds.length > 0 && submittedRunIds.every((id) => {
      const run = data.runs.find((item) => item.id === id);
      return run && !isActiveImportRun(run);
    });
    if (finishedUploads) setImportNotice(null);
  }, [data, submittedRunIds]);

  useEffect(() => {
    if (!importOpen) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape" && !importing) setImportOpen(false);
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [importOpen, importing]);

  useEffect(() => {
    if (!deleteTarget || deletingSource) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setDeleteTarget(null);
        setDeleteError(null);
      }
    }
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget, deletingSource]);

  function openImport(tab: ImportTab = "file") {
    if (busy || operation.current) return;
    setImportTab(tab);
    setImportError(null);
    setImportProgress(null);
    setImportOpen(true);
  }

  async function uploadFiles(files: ImportFile[], batch = false) {
    if (busy || operation.current) return;
    const { supported, skipped } = prepareImportFiles(files);
    if (!supported.length) {
      setImportError(skipped.length
        ? `没有可导入的文件，以下文件已跳过：\n${skipped.join("\n")}`
        : "未找到文件，请选择包含受支持文档的文件夹。");
      return;
    }
    operation.current = "import";
    setImporting(true);
    setImportError(null);
    setImportNotice(null);
    setSubmittedRunIds([]);
    let saved = 0;
    let queued = 0;
    const failures: string[] = skipped.map((path) => `已跳过 ${path}`);
    const pendingRuns: string[] = [];
    try {
      if (batch || files.length > 1) {
        setImportProgress(`正在整批导入 ${supported.length} 份文档…`);
        const outcome = batchIngestResult(
          await api.uploadSourceBatch(supported.map((item) => item.file)), supported.length
        );
        saved = outcome.saved;
        queued = outcome.queued;
        failures.push(...outcome.failures);
        if (outcome.runId) pendingRuns.push(outcome.runId);
      } else {
        const item = supported[0];
        setImportProgress(`正在导入：${item.path}`);
        const result = await api.uploadSource(item.file);
        saved = 1;
        const outcome = ingestResult(result);
        if (outcome.queued) {
          queued = 1;
          pendingRuns.push(result.compilation_run_id);
        } else {
          failures.push(`${item.path}：${outcome.detail}`);
        }
      }
      if (queued) {
        setSubmittedRunIds(pendingRuns);
        setImportNotice(`${queued} 份资料已保存，同一个编译任务已排队；完成后统一审核发布或拒绝。`);
      }
      if (failures.length) {
        setImportError(failures.join("\n"));
      } else {
        setImportOpen(false);
      }
      if (saved) reload();
    } catch (cause) {
      setImportError([...failures, `整次导入未成功提交，未确认已保存或排队：${uploadErrorMessage(cause)}`].join("\n"));
      reload();
    } finally {
      operation.current = null;
      setImporting(false);
      setImportProgress(null);
    }
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    const files = event.target.files ? selectedFiles(event.target.files) : [];
    const batch = event.target.hasAttribute("webkitdirectory");
    event.target.value = "";
    void uploadFiles(files, batch);
  }

  async function dropFiles(event: DragEvent<HTMLLabelElement>) {
    event.preventDefault();
    setDragging(false);
    try {
      await uploadFiles(await droppedFiles(event.dataTransfer), true);
    } catch (cause) {
      setImportError(cause instanceof Error ? cause.message : "无法读取拖入的文件夹，请用文件夹选择器重试。");
    }
  }

  async function retrySource(source: Source) {
    if (busy || operation.current || !source.current_revision_id) return;
    if (!canRetrySource(sourceProcessingStatus(source, data?.runs ?? [], data?.drafts ?? []), source)) return;
    operation.current = "compile";
    setRetryingSourceId(source.id);
    setSourceActionError(null);
    try {
      const run = await api.retrySource(source.id);
      setSubmittedRunIds((ids) => [...ids, run.id]);
      setData((current) => current
        ? { ...current, runs: [run, ...current.runs] }
        : current);
      setImportNotice("已重新提交编译任务，请在左侧查看处理状态。");
      reload();
    } catch (cause) {
      setSourceActionError({ id: source.id, message: compilationErrorMessage(cause) });
      reload();
    } finally {
      operation.current = null;
      setRetryingSourceId(null);
    }
  }

  async function deleteSource() {
    if (!deleteTarget || busy || operation.current) return;
    operation.current = "delete";
    const target = deleteTarget;
    setDeleteTarget(null);
    setDeletingSource(true);
    setDeleteError(null);
    try {
      await api.deleteSource(target.id);
      const removedId = target.id;
      setCheckedSourceIds((ids) => {
        const next = new Set(ids);
        next.delete(removedId);
        return next;
      });
      setData((current) => current
        ? { ...current, sources: current.sources.filter((item) => item.id !== removedId) }
        : current);
      if (selectedSourceId === removedId) {
        setSelectedSourceId(null);
        setSelectedRevisionId(null);
        setSelectedSpanId(null);
      }
      setSourceActionError(null);
      setImportNotice(null);
      reload();
    } catch (cause) {
      setDeleteError(deletionErrorMessage(cause));
      reload();
    } finally {
      operation.current = null;
      setDeletingSource(false);
    }
  }

  function importWebsite(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || operation.current) return;
    openBrowser(websiteUrl);
  }

  function openBrowser(value: string) {
    if (busy || operation.current) return;
    try {
      const payload = websiteImportPayload(value, "page");
      operation.current = "import";
      setBrowserCleanupError(null);
      setImportError(null);
      setImportNotice(null);
      setSubmittedRunIds([]);
      setImportOpen(false);
      setBrowserUrl(payload.url);
    } catch (cause) {
      setImportError(cause instanceof Error ? cause.message : "网址无效，请检查后重试。");
    }
  }

  function browserImported(source: Source) {
    operation.current = null;
    setBrowserUrl(null);
    setWebsiteUrl("");
    setData((current) => current ? {
      ...current, sources: [source, ...current.sources.filter((item) => item.id !== source.id)]
    } : current);
    setSelectedSourceId(source.id);
    setSelectedRevisionId(null);
    setSelectedSpanId(null);
    setImportNotice(`已导入“${source.name}”的正文，可点击“编译”生成 Wiki 页面。`);
    reload();
  }

  function openRename(source: Source, location: "list" | "preview" = "list") {
    if (busy || operation.current) return;
    renameCancelled.current = false;
    setRenameTarget(source);
    setRenameLocation(location);
    setRenameName(source.name);
    setRenameError(null);
  }

  async function renameSource() {
    if (!renameTarget || busy || operation.current || renameCancelled.current) return;
    let name: string;
    try {
      name = validateSourceName(renameName);
    } catch (cause) {
      setRenameError(cause instanceof Error ? cause.message : "名称无效。");
      return;
    }
    if (name === renameTarget.name) {
      setRenameTarget(null);
      setRenameError(null);
      return;
    }
    operation.current = "rename";
    setRenamingSource(true);
    setRenameError(null);
    try {
      const source = await api.renameSource(renameTarget.id, name);
      setData((current) => current ? {
        ...current, sources: current.sources.map((item) => item.id === source.id ? source : item)
      } : current);
      setRenameTarget(null);
      reload();
    } catch (cause) {
      setRenameError(cause instanceof Error ? cause.message : "重命名失败，请稍后重试。");
    } finally {
      operation.current = null;
      setRenamingSource(false);
    }
  }

  function nameEditor(source: Source | undefined, location: "list" | "preview"): ReactNode {
    if (!source || renameTarget?.id !== source.id || renameLocation !== location) return null;
    return (
      <div className="source-name-field">
        <input
          aria-label="文件名"
          autoFocus
          className="source-name-input"
          disabled={renamingSource}
          onBlur={() => void renameSource()}
          onChange={(event) => setRenameName(event.target.value)}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => {
            event.stopPropagation();
            if (event.key === "Enter") {
              event.preventDefault();
              void renameSource();
            } else if (event.key === "Escape" && !operation.current) {
              event.preventDefault();
              renameCancelled.current = true;
              setRenameTarget(null);
              setRenameError(null);
            }
          }}
          title="Enter 保存，Esc 取消"
          value={renameName}
        />
        {renameError ? <span className="source-name-error" role="alert">{renameError}</span> : null}
      </div>
    );
  }

  async function compileSelectedSources() {
    if (busy || operation.current || !compilationPlan.ready.length) return;
    operation.current = "compile";
    setBatchAction("compile");
    setBatchReport(null);
    const { ready, skipped } = compilationPlan;
    const skippedMessages = skipped.map((source) =>
      `已跳过“${source.name}”：正在处理、等待审核、已发布或无可编译版本。`);
    try {
      setBatchProgress(`正在整批提交 ${ready.length} 份资料的统一编译…`);
      const run = ready.length > 1
        ? await api.compileSourceBatch(ready.map((source) => source.id))
        : await api.retrySource(ready[0].id);
      if (run.status !== "queued" || !run.id) {
        throw new Error("编译任务未进入队列，请刷新检查任务状态。");
      }
      const revisionIds = sourceRunRevisionIds(run);
      if (!ready.every((source) => source.current_revision_id && revisionIds.includes(source.current_revision_id))) {
        throw new Error("未确认本批次全部资料进入同一个编译任务，请刷新检查任务状态。");
      }
      setSubmittedRunIds((ids) => [...ids, run.id]);
      setData((current) => current ? { ...current, runs: [run, ...current.runs] } : current);
      setBatchReport({
        failed: false,
        message: [
          `批量编译：已提交 ${revisionIds.length} 份，跳过 ${skipped.length} 份，失败 0 份。统一生成一份待审核草稿。`,
          ...skippedMessages
        ].join("\n")
      });
    } catch (cause) {
      setBatchReport({
        failed: true,
        message: [
          `批量编译：已提交 0 份，跳过 ${skipped.length} 份，失败 ${ready.length} 份。整批未成功进入队列，未逐份重试。`,
          compilationErrorMessage(cause),
          ...skippedMessages
        ].join("\n")
      });
    } finally {
      reload();
      operation.current = null;
      setBatchAction(null);
      setBatchProgress(null);
    }
  }

  async function deleteSelectedSources() {
    if (!bulkDeleteTargets?.length || busy || operation.current) return;
    operation.current = "delete";
    setBatchAction("delete");
    setBatchReport(null);
    const targets = bulkDeleteTargets;
    try {
      const result = await runSourceBatch(targets, async (source) => {
        await api.deleteSource(source.id);
        setData((current) => current ? {
          ...current, sources: current.sources.filter((item) => item.id !== source.id)
        } : current);
        setCheckedSourceIds((ids) => {
          const next = new Set(ids);
          next.delete(source.id);
          return next;
        });
        if (selectedSourceId === source.id) {
          setSelectedSourceId(null);
          setSelectedRevisionId(null);
          setSelectedSpanId(null);
        }
      }, deletionErrorMessage, (source, index) =>
        setBatchProgress(`正在删除 ${index + 1} / ${targets.length}：${source.name}`)
      );
      setBatchReport({
        failed: result.failed.length > 0,
        message: [
          `批量删除：成功 ${result.succeeded.length} 份，失败 ${result.failed.length} 份。失败项仍保留选中，可重试。`,
          ...result.failed.map(({ source, message }) => `“${source.name}”删除失败：${message}`)
        ].join("\n")
      });
      setBulkDeleteTargets(null);
      reload();
    } finally {
      operation.current = null;
      setBatchAction(null);
      setBatchProgress(null);
    }
  }

  return (
    <>
      <RequestState error={error} loading={loading && !data} onRetry={reload} />
      {deleteError ? <div className="form-error" role="alert">{deleteError}</div> : null}
      {selectionError ? <div className="form-error" role="alert">{selectionError}</div> : null}
      {browserCleanupError ? <div className="form-error" role="alert">{browserCleanupError}</div> : null}
      {data ? (
        <div className="wiki-workspace">
          <aside className="wiki-sources-panel">
            <div className="wiki-sources-head">
              <div>
                <strong>文件导入</strong>
                {data.sources.length ? <span>{data.sources.length}</span> : null}
              </div>
              <button
                aria-label="导入文件或网址"
                className="source-import-button"
                disabled={busy}
                onClick={() => openImport()}
                title="导入文件或网址"
                type="button"
              >
                +
              </button>
            </div>
            {failedUpload ? (
              <div className="source-notice source-notice-error" role="alert">
                <span>
                  {`刚导入的资料处理失败：${failedUpload.status === "published_index_failed"
                    ? "资料已发布，但搜索索引构建失败，请重建索引。"
                    : importFailureMessage(failedUpload.error)}`}
                </span>
                <button
                  aria-label="关闭提示"
                  onClick={() => {
                    setImportNotice(null);
                    setSubmittedRunIds([]);
                  }}
                  type="button"
                >×</button>
              </div>
            ) : importNotice ? (
              <div className="source-notice" role="status">
                <span>{importNotice}</span>
                <button aria-label="关闭提示" onClick={() => setImportNotice(null)} type="button">×</button>
              </div>
            ) : null}
            <div className="source-search">
              <input
                aria-label="搜索原件"
                onChange={(event) => setSourceQuery(event.target.value)}
                placeholder="搜索已导入原件…"
                type="search"
                value={sourceQuery}
              />
            </div>
            <div className="source-selection-toolbar" aria-label="来源批量操作">
              <label className="source-select-all">
                <input
                  aria-label="全选当前列表"
                  aria-checked={visibleSelection.mixed ? "mixed" : visibleSelection.all}
                  checked={visibleSelection.all}
                  disabled={busy || !filteredSources.length}
                  onChange={() => setCheckedSourceIds((ids) => toggleVisibleSources(filteredSources, ids))}
                  ref={selectAllInput}
                  type="checkbox"
                />
                全选
              </label>
              <span className={checkedSources.length ? "source-selection-count source-selection-count-active" : "source-selection-count"}
                aria-live="polite" title={`当前搜索结果选中 ${visibleSelection.count} 份，共选中 ${checkedSources.length} 份`}>
                {checkedSources.length ? `已选 ${checkedSources.length}` : `${filteredSources.length} 个文件`}
              </span>
              <div className="source-batch-actions">
                <button aria-label={`批量编译（${compilationPlan.ready.length}）`}
                  className="source-batch-button" disabled={busy || !compilationPlan.ready.length}
                  title={`编译选中的 ${compilationPlan.ready.length} 份资料${compilationPlan.skipped.length ? `，跳过 ${compilationPlan.skipped.length} 份不符合条件的资料` : ""}`}
                  onClick={() => void compileSelectedSources()} type="button">
                  <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" viewBox="0 0 24 24" width="14">
                    <path d="m8 5 11 7-11 7Z" />
                  </svg>
                  编译
                </button>
                <button aria-label="批量删除" className="source-batch-button source-batch-button-delete"
                  disabled={busy || !checkedSources.length} title={`删除选中的 ${checkedSources.length} 份资料`}
                  onClick={() => setBulkDeleteTargets(checkedSources)} type="button">
                  <svg aria-hidden="true" fill="none" height="14" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.6" viewBox="0 0 24 24" width="14">
                    <path d="M4 7h16M10 4h4m-8 3 1 13h10l1-13M10 11v6m4-6v6" />
                  </svg>
                  删除
                </button>
              </div>
            </div>
            {batchProgress ? <div className="source-batch-report" role="status">{batchProgress}</div> : null}
            {batchReport ? (
              <div className={`source-batch-report${batchReport.failed ? " source-status-danger" : ""}`}
                role={batchReport.failed ? "alert" : "status"}>
                {batchReport.message}
                <button aria-label="关闭批量操作结果" onClick={() => setBatchReport(null)} type="button">×</button>
              </div>
            ) : null}
            <div className="source-list">
              {filteredSources.length === 0 ? (
                <div className="source-empty">
                  <strong>{data.sources.length ? "没有匹配的来源" : "还没有知识来源"}</strong>
                  <p>{data.sources.length ? "尝试其他关键词。" : "导入文档，系统会自动编译进 Wiki。"}</p>
                  {!data.sources.length ? (
                    <button className="button button-small" disabled={busy} onClick={() => openImport()} type="button">
                      导入文档
                    </button>
                  ) : null}
                </div>
              ) : (
                filteredSources.map((source) => (
                  <SourceItem
                    key={source.id}
                    source={source}
                    selected={selectedSourceId === source.id}
                    checked={checkedSourceIds.has(source.id)}
                    onCheck={() => setCheckedSourceIds((ids) => {
                      const next = new Set(ids);
                      if (next.has(source.id)) next.delete(source.id);
                      else next.add(source.id);
                      return next;
                    })}
                    onOpen={() => {
                      setSelectedSourceId(source.id);
                      setSelectedRevisionId(null);
                      setSelectedSpanId(null);
                    }}
                    status={sourceProcessingStatus(source, data.runs, data.drafts)}
                    retrying={retryingSourceId === source.id}
                    busy={busy}
                    error={sourceActionError?.id === source.id ? sourceActionError.message : null}
                    onRetry={() => void retrySource(source)}
                    onDelete={() => { setDeleteError(null); setDeleteTarget(source); }}
                    onRename={() => openRename(source)}
                    titleEditor={nameEditor(source, "list")}
                  />
                ))
              )}
            </div>
          </aside>

          <section className="wiki-main-panel">
            <SourcePreview
              source={data.sources.find((source) => source.id === selectedSourceId) ?? null}
              revisionId={selectedRevisionId}
              spanId={selectedSpanId}
              busy={busy}
              onRename={(source) => openRename(source, "preview")}
              titleEditor={selectedSourceId
                ? nameEditor(data.sources.find((source) => source.id === selectedSourceId), "preview")
                : null}
            />
          </section>
        </div>
      ) : null}

      {importOpen ? (
        <div
          className="settings-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !importing) setImportOpen(false);
          }}
        >
          <section
            aria-labelledby="import-dialog-title"
            aria-modal="true"
            className="settings-modal import-source-dialog"
            role="dialog"
          >
            <div className="settings-modal-head">
              <div>
                <span className="eyebrow">添加知识来源</span>
                <h2 id="import-dialog-title">导入知识</h2>
              </div>
              <button
                aria-label="关闭导入窗口"
                disabled={importing}
                onClick={() => setImportOpen(false)}
                type="button"
              >
                ×
              </button>
            </div>
            <div className="import-tabs" role="tablist" aria-label="导入方式">
              <button
                aria-selected={importTab === "file"}
                className={importTab === "file" ? "import-tab import-tab-active" : "import-tab"}
                onClick={() => setImportTab("file")}
                role="tab"
                type="button"
              >
                文件上传
              </button>
              <button
                aria-selected={importTab === "url"}
                className={importTab === "url" ? "import-tab import-tab-active" : "import-tab"}
                onClick={() => setImportTab("url")}
                role="tab"
                type="button"
              >
                网址导入
              </button>
            </div>

            {importTab === "file" ? (
              <div>
                <label
                  className={dragging ? "import-dropzone import-dropzone-active" : "import-dropzone"}
                  onDragEnter={(event) => {
                    event.preventDefault();
                    setDragging(true);
                  }}
                  onDragLeave={() => setDragging(false)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={dropFiles}
                >
                  <input
                    accept=".pdf,.docx,.md,.txt,.html,.htm"
                    disabled={importing || !uploadEnabled}
                    multiple
                    onChange={chooseFiles}
                    type="file"
                  />
                  <span className="import-dropzone-icon" aria-hidden="true">+</span>
                  <strong>{dragging ? "松开鼠标开始导入" : "拖拽文档到这里，或点击选择"}</strong>
                  <small>支持 PDF、DOCX、Markdown、TXT 和 HTML，可批量选择或拖入文件夹</small>
                </label>
                <label className="import-folder-button">
                  <input
                    aria-label="选择文件夹"
                    disabled={importing || !uploadEnabled}
                    multiple
                    onChange={chooseFiles}
                    type="file"
                    {...{ webkitdirectory: "" }}
                  />
                  选择文件夹（含子文件夹）
                </label>
                <p className="import-help">不支持的格式及空文件会列出并跳过；其余文档一起编译为一份草稿，统一审核发布或拒绝。</p>
                {!uploadEnabled ? (
                  <p className="import-help">文档导入当前不可用。</p>
                ) : null}
              </div>
            ) : (
              <form className="website-import-form" onSubmit={(event) => void importWebsite(event)}>
                <label className="field field-wide">
                  网址
                  <input
                    disabled={importing || !crawlEnabled}
                    name="url"
                    placeholder="https://docs.example.com/"
                    required
                    type="url"
                    maxLength={8192}
                    value={websiteUrl}
                    onChange={(event) => setWebsiteUrl(event.target.value)}
                  />
                </label>
                {!crawlEnabled ? (
                  <p className="import-help">网址导入当前不可用。</p>
                ) : (
                  <p className="import-help">开始后会打开实时网页。在网页中完成验证或登录，浏览到想要保存的页面，再点击确认导入。</p>
                )}
                <div className="settings-modal-actions">
                  <button className="button button-secondary" disabled={importing} onClick={() => setImportOpen(false)} type="button">
                    取消
                  </button>
                  <button className="button" disabled={importing || !crawlEnabled} type="submit">
                    {importing ? "正在导入…" : "开始导入"}
                  </button>
                </div>
              </form>
            )}

            {importProgress ? <div className="form-notice" role="status">{importProgress}</div> : null}
            {importError ? <div className="form-error import-file-errors" role="alert">{importError}</div> : null}
          </section>
        </div>
      ) : null}
      {browserUrl ? (
        <WebBrowserImport url={browserUrl} onImported={browserImported}
          onClosed={() => { operation.current = null; setBrowserUrl(null); }} onCleanupError={setBrowserCleanupError} />
      ) : null}
      {bulkDeleteTargets ? (
        <div className="settings-modal-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !batchAction) setBulkDeleteTargets(null);
        }}>
          <div aria-labelledby="bulk-delete-source-title" aria-describedby="bulk-delete-source-description"
            aria-modal="true" className="settings-modal settings-modal-small" role="dialog">
            <div className="danger-dialog-icon" aria-hidden="true">!</div>
            <h2 id="bulk-delete-source-title">删除 {bulkDeleteTargets.length} 份知识来源？</h2>
            <p id="bulk-delete-source-description">将删除以下原件及其相关 Wiki 引用。删除无法撤销。</p>
            <ul className="source-bulk-delete-names">
              {bulkDeleteTargets.map((source) => <li key={source.id}>{source.name}</li>)}
            </ul>
            {batchProgress ? <div role="status">{batchProgress}</div> : null}
            <div className="settings-modal-actions">
              <button autoFocus className="button button-secondary" disabled={busy}
                onClick={() => setBulkDeleteTargets(null)} type="button">取消</button>
              <button className="button button-danger" disabled={busy}
                onClick={() => void deleteSelectedSources()} type="button">
                {batchAction === "delete" ? "正在删除…" : `确认删除 ${bulkDeleteTargets.length} 份`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {deleteTarget ? (
        <div
          className="settings-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !deletingSource) setDeleteTarget(null);
          }}
          role="presentation"
        >
          <div
            aria-labelledby="delete-source-title"
            aria-describedby="delete-source-description"
            aria-modal="true"
            className="settings-modal settings-modal-small"
            onMouseDown={(event) => event.stopPropagation()}
            role="dialog"
          >
            <div className="danger-dialog-icon" aria-hidden="true">!</div>
            <h2 id="delete-source-title">删除知识来源？</h2>
            <p id="delete-source-description">
              “{deleteTarget.name}”
              <br />
              删除无法撤销。
            </p>
            <div className="settings-modal-actions">
              <button autoFocus className="button button-secondary" disabled={deletingSource}
                onClick={() => setDeleteTarget(null)} type="button">取消</button>
              <button className="button button-danger" disabled={deletingSource}
                onClick={() => void deleteSource()} type="button">
                {deletingSource ? "正在删除…" : "确认删除"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

function SourceItem({
  source, status, selected, checked, retrying, busy, error, onOpen, onCheck, onRetry, onDelete, onRename, titleEditor
}: {
  source: Source;
  status: SourceProcessingStatus;
  selected: boolean;
  checked: boolean;
  retrying: boolean;
  busy: boolean;
  error: string | null;
  onOpen: () => void;
  onCheck: () => void;
  onRetry: () => void;
  onDelete: () => void;
  onRename: () => void;
  titleEditor: ReactNode;
}) {
  return (
    <article
      aria-label={`查看原件：${source.name}`}
      className={`source-item${selected ? " source-item-selected" : ""}`}
      onClick={(event) => {
        if ((event.target as HTMLElement).closest("button, a, input, label")) return;
        onOpen();
      }}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget || (event.key !== "Enter" && event.key !== " ")) return;
        event.preventDefault();
        onOpen();
      }}
      role="button"
      tabIndex={0}
    >
      <label className="source-item-selection">
        <input aria-label={`选择来源：${source.name}`} checked={checked} disabled={busy}
          onChange={onCheck} type="checkbox" />
        <span className="source-kind" aria-hidden="true">
          {source.kind === "website" ? "网" : "文"}
        </span>
      </label>
      <div>
        {titleEditor || (
          <button
            aria-label={`修改文件名：${source.name}`}
            className="source-name-title"
            disabled={busy}
            onClick={(event) => { event.stopPropagation(); onRename(); }}
            title="点击文件名修改，Enter 保存，Esc 取消"
            type="button"
          >
            {source.name}
          </button>
        )}
        <span>{formatLabel(source.kind)} · {formatDateTime(source.created_at)}</span>
      </div>
      <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
      {status.tone !== "positive" ? (
        <p className={`source-status-detail${status.tone === "danger" ? " source-status-danger" : ""}`}
          role={status.tone === "danger" ? "alert" : undefined}>
          {status.detail}
          {status.action ? (
            <> <Link href={status.action.href} onClick={(event) => event.stopPropagation()}>{status.action.label}</Link></>
          ) : null}
        </p>
      ) : null}
      <div className="source-item-actions">
        {canRetrySource(status, source) ? (
          <button disabled={busy} onClick={(event) => { event.stopPropagation(); onRetry(); }} type="button">
            {retrying ? "提交中…" : status.label === "未编译" ? "编译" : "重试"}
          </button>
        ) : null}
        <button className="source-item-delete" disabled={busy}
          onClick={(event) => { event.stopPropagation(); onDelete(); }} type="button">
          删除
        </button>
      </div>
      {error ? <p className="source-status-detail source-status-danger" role="alert">{error}</p> : null}
    </article>
  );
}

function SourcePreview({
  source,
  revisionId,
  spanId,
  busy,
  onRename,
  titleEditor
}: {
  source: Source | null;
  revisionId: string | null;
  spanId: string | null;
  busy: boolean;
  onRename: (source: Source) => void;
  titleEditor: ReactNode;
}) {
  const [preview, setPreview] = useState<{
    url: string;
    text: string | null;
    spans: SourceSpan[];
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [contentMode, setContentMode] = useState<"rich" | "markdown">("rich");
  const citedSpan = preview?.spans.find((span) => span.id === spanId) ?? null;

  useEffect(() => {
    setPreview(null);
    setError(null);
    setContentMode("rich");
    if (!source || (!revisionId && !source.current_revision_id)) {
      setLoading(false);
      return;
    }
    const selectedSource = source;
    const selectedRevision = revisionId ?? source.current_revision_id;
    if (!selectedRevision) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoading(true);
    async function load(revision: string) {
      try {
        const [blob, spans] = await Promise.all([
          api.readSourceContent(revision),
          api.listSourceSpans(revision)
        ]);
        if (cancelled) return;
        const isText = ["text", "markdown", "html", "website"].includes(selectedSource.kind);
        const text = selectedSource.kind === "website" && !spans.length
          ? await (await api.readWebsitePreview(revision)).text()
          : isText ? await blob.text() : null;
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPreview({
          url: objectUrl,
          text,
          spans
        });
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "无法读取原件。");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void load(selectedRevision);
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [source?.id, source?.current_revision_id, source?.kind, revisionId, retry]);

  if (!source) {
    return <div className="source-preview-empty">选择左侧原件，即可查看文件内容。</div>;
  }

  return (
    <div className="source-preview">
      <div className="wiki-main-head">
        <div>
          {titleEditor || (
            <button aria-label={`修改文件名：${source.name}`} className="source-name-title"
              disabled={busy} onClick={() => onRename(source)} title="点击文件名修改"
              type="button">{source.name}</button>
          )}
          <span>{formatLabel(source.kind)} · {revisionId ? "引用版本" : "原件预览"}</span>
        </div>
        <div className="source-preview-actions">
          {preview ? <a className="button button-small" download={source.name} href={preview.url}>下载原件</a> : null}
        </div>
      </div>
      {!revisionId && !source.current_revision_id ? (
        <p className="source-preview-message">该来源暂无可查看的原件版本。</p>
      ) : (
        <>
          <RequestState error={error} loading={loading} onRetry={() => setRetry((value) => value + 1)} />
          {preview ? (
            <>
              {spanId ? (
                <section className="source-citation-focus" aria-label="引用的原文片段">
                  <strong>{citedSpan ? `引用原文 · 片段 ${citedSpan.ordinal + 1}` : "未找到对应的原文片段"}</strong>
                  {citedSpan ? (
                    <>
                      <div className="wiki-rendered">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{citedSpan.content}</ReactMarkdown>
                      </div>
                    </>
                  ) : (
                    <p>原件仍可在下方查看，但该引用片段已不存在。</p>
                  )}
                </section>
              ) : null}
              <div className="wiki-reader-toolbar" role="group" aria-label="原件内容显示方式">
                <button aria-pressed={contentMode === "rich"}
                  onClick={() => setContentMode("rich")} type="button">富文本</button>
                <button aria-pressed={contentMode === "markdown"}
                  onClick={() => setContentMode("markdown")} type="button">Markdown</button>
              </div>
              {contentMode === "markdown" ? (
                <pre className="wiki-markdown-source"><code>{sourceMarkdown(preview)}</code></pre>
              ) : sourceMarkdown(preview) ? (
                <div className="wiki-rendered source-preview-rich">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{sourceMarkdown(preview)}</ReactMarkdown>
                </div>
              ) : source.kind === "pdf" ? (
                <iframe className="source-preview-pdf" src={preview.url} title={`${source.name} 原件`} />
              ) : (
                <p className="source-preview-message">原件尚未解析出可预览文本，可下载查看完整文件。</p>
              )}
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
