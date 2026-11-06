"use client";

import {
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  useApiResource
} from "@wiki-agent/web-ui";
import Link from "next/link";
import { useCallback, useRef, useState } from "react";
import {
  checkImport,
  createImportRequest,
  loadDingtalkCatalog,
  publicationNotice,
  submitImport,
  wikiPathLink,
  type ImportOutcome
} from "./import";

export default function DingtalkPage() {
  const load = useCallback(() => loadDingtalkCatalog(api), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [selectedDocument, setSelectedDocument] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  // This also blocks a second click before React has rendered the disabled button.
  const actionInFlight = useRef(false);

  async function importSelected() {
    if (actionInFlight.current || outcome || !data?.catalog) return;
    const request = createImportRequest(data.catalog, selectedDocument, () => crypto.randomUUID());
    if (!request) return;
    actionInFlight.current = true;
    setBusy(true);
    setOutcome({ request_id: request.request_id, wiki_readable: false });
    try {
      setOutcome(await submitImport(request, api));
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  }

  async function checkStatus() {
    if (!outcome || actionInFlight.current) return;
    actionInFlight.current = true;
    setBusy(true);
    try {
      setOutcome(await checkImport(outcome, api));
    } finally {
      actionInFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        description="仅使用本地固定模拟文档，不连接钉钉开放平台，不需要真实钉钉凭据。"
        eyebrow="离线模拟"
        title="模拟钉钉导入"
      />
      <Section title="集成状态">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {data?.status.mode === "disabled" ? (
          <EmptyState title="模拟功能未启用；真实钉钉尚未实现" description="需由服务端显式启用 mock 模式后才能选择模拟文档。" />
        ) : null}
        {data?.status.mode === "mock" ? (
          <>
            <StatusBadge tone="warning">模拟钉钉，未连接真实钉钉</StatusBadge>
            <p className="muted">数据版本：{data.status.fixture_version}；真实钉钉尚未实现。</p>
          </>
        ) : null}
      </Section>
      {data?.catalog ? (
        <Section subtitle={data.catalog.notice} title="选择一篇模拟文档">
          <fieldset disabled={busy || outcome !== null}>
            <legend>单选文档；初始不选择，确认后只导入所选一篇。</legend>
            {data.catalog.items.map((item) => (
              <label className="field" key={item.document_id}>
                <span>
                  <input
                    checked={selectedDocument === item.document_id}
                    name="dingtalk-document"
                    onChange={() => setSelectedDocument(item.document_id)}
                    type="radio"
                    value={item.document_id}
                  />{" "}
                  {item.title} <span className="muted">({item.document_id})</span>
                </span>
              </label>
            ))}
          </fieldset>
          <div className="form-actions">
            <button
              className="button"
              disabled={busy || !selectedDocument || outcome !== null}
              onClick={() => void importSelected()}
              type="button"
            >
              {busy ? "处理中…" : "导入所选模拟文档"}
            </button>
          </div>
        </Section>
      ) : null}
      {outcome ? (
        <Section title="本次模拟导入结果">
          <StatusBadge tone="warning">模拟钉钉 · 未连接真实钉钉</StatusBadge>
          <p role="status">{busy ? "正在核验本次动作…" : publicationNotice(outcome)}</p>
          {outcome.message ? <p className="form-error" role="alert">{outcome.message}</p> : null}
          <dl>
            <dt>查询 ID</dt><dd><code>{outcome.request_id}</code></dd>
            <dt>运行状态</dt><dd>{outcome.state ?? "待查询"}</dd>
            {outcome.page_path ? <><dt>Wiki 路径</dt><dd><code>{outcome.page_path}</code></dd></> : null}
            {outcome.published_commit ? <><dt>发布 commit</dt><dd><code>{outcome.published_commit}</code></dd></> : null}
          </dl>
          <p className="muted">状态查询仅执行只读 GET，不创建新 UUID，不重新取源或发布。</p>
          <div className="form-actions">
            <button className="button button-secondary" disabled={busy} onClick={() => void checkStatus()} type="button">
              只读查询本次状态
            </button>
            {outcome.page_path ? (
              <Link className="button button-secondary" href={wikiPathLink(outcome.page_path)}>到Wiki重新打开</Link>
            ) : null}
            {outcome.state === "published" && outcome.wiki_readable ? (
              <button
                className="button button-secondary"
                disabled={busy}
                onClick={() => { setOutcome(null); setSelectedDocument(null); }}
                type="button"
              >
                开始另一次模拟导入
              </button>
            ) : null}
          </div>
        </Section>
      ) : null}
    </>
  );
}
