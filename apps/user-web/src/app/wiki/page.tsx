"use client";

import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  EmptyState,
  RequestState,
  StatusBadge,
  api,
  formatDateTime,
  formatLabel,
  type WikiPage,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback, useEffect, useRef, useState } from "react";
import { renderWikiCitationMarkers, wikiCitationEntries } from "./citations";
import { graphNeighbors } from "./graph-layout";
import { WikiGraphCanvas } from "./wiki-graph";

export default function WikiPageView() {
  const load = useCallback(() => api.getWikiGraph(), []);
  const { data: graph, error, loading, reload } = useApiResource(load);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [sourceNames, setSourceNames] = useState<Record<string, string>>({});
  const [sourceLookupError, setSourceLookupError] = useState<string | null>(null);
  const [contentMode, setContentMode] = useState<"rich" | "markdown">("rich");
  const pageRequest = useRef(0);
  const requestedPath = useRef<string | null>(null);

  const closePage = useCallback(() => {
    ++pageRequest.current;
    setSelectedPath(null);
    setPage(null);
    setPageError(null);
    setSourceNames({});
    setSourceLookupError(null);
    setPageLoading(false);
  }, []);

  const selectPage = useCallback(async (path: string) => {
    const request = ++pageRequest.current;
    setSelectedPath(path);
    setContentMode("rich");
    setPage(null);
    setPageError(null);
    setSourceNames({});
    setSourceLookupError(null);
    setPageLoading(true);
    try {
      const loaded = await api.readWikiPage(path);
      if (request === pageRequest.current) setPage(loaded);
      if (Object.keys(loaded.metadata.citations).length) {
        try {
          const sourceList = await api.listSources();
          const revisionIds = [...new Set(
            Object.values(loaded.metadata.citations).map((citation) => citation.source_revision_id)
          )];
          const revisions = await Promise.all(
            revisionIds.map(async (revisionId) => ({
              revisionId,
              revision: await api.getSourceRevision(revisionId)
            }))
          );
          if (request === pageRequest.current) {
            setSourceNames(Object.fromEntries(revisions.flatMap(({ revisionId, revision }) => {
              const source = sourceList.find((item) => item.id === revision.source_id);
              return source ? [[revisionId, source.name]] : [];
            })));
          }
        } catch (cause) {
          if (request === pageRequest.current) {
            setSourceLookupError(
              cause instanceof Error ? cause.message : "无法查找引用的原始资料。"
            );
          }
        }
      }
    } catch (cause) {
      if (request === pageRequest.current) {
        setPageError(cause instanceof Error ? cause.message : "无法加载知识页面。");
      }
    } finally {
      if (request === pageRequest.current) setPageLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!graph) return;
    const path = new URLSearchParams(window.location.search).get("page");
    if (!path || path === requestedPath.current) return;
    requestedPath.current = path;
    void selectPage(path);
  }, [graph, selectPage]);

  useEffect(() => {
    if (!selectedPath) return;
    function onEscape(event: KeyboardEvent) {
      if (event.key === "Escape") closePage();
    }
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [selectedPath, closePage]);

  return (
    <>
      <RequestState error={error} loading={loading && !graph} onRetry={reload} />
      {graph ? (
        <section className="wiki-workspace wiki-graph-workspace">
          {graph.nodes.length ? (
            <div className={`wiki-graph-layout${selectedPath ? " wiki-graph-layout-reading" : ""}`}>
              <WikiGraphCanvas graph={graph} onOpen={(path) => void selectPage(path)}
                selectedPath={selectedPath} />
              {selectedPath ? (
                <aside aria-labelledby="wiki-reader-title" className="wiki-reader-panel">
                  <div className="wiki-reader-panel-head">
                    <div>
                      <span className="eyebrow">
                        知识 Wiki · {page ? formatLabel(page.metadata.type) : "页面"}
                      </span>
                      <h2 id="wiki-reader-title">{page?.metadata.title ?? "正在加载页面…"}</h2>
                    </div>
                    <button aria-label="关闭知识页面" onClick={closePage} type="button">×</button>
                  </div>
                  <RequestState error={pageError} loading={pageLoading}
                    onRetry={() => void selectPage(selectedPath)} />
                  {page ? (
                    <div className="wiki-reader-panel-body">
                      <div className="wiki-reader-meta">
                        <StatusBadge tone="positive">{formatLabel(page.metadata.status)}</StatusBadge>
                        <span>更新于 {formatDateTime(page.metadata.updated)}</span>
                        {Object.keys(page.metadata.citations).length
                          ? <span>{Object.keys(page.metadata.citations).length} 条引用</span>
                          : null}
                      </div>
                      <div className="wiki-reader-toolbar" role="group" aria-label="内容显示方式">
                        <button aria-pressed={contentMode === "rich"}
                          onClick={() => setContentMode("rich")} type="button">富文本</button>
                        <button aria-pressed={contentMode === "markdown"}
                          onClick={() => setContentMode("markdown")} type="button">Markdown</button>
                      </div>
                      {contentMode === "rich" ? (
                        <div className="wiki-rendered">
                          <ReactMarkdown
                            components={{
                              a: ({ href, children, ...props }) => href?.startsWith("#wiki-citation-")
                                ? (
                                  <sup>
                                    <a href={href} {...props}>{children}</a>
                                  </sup>
                                )
                                : <a href={href} {...props}>{children}</a>
                            }}
                            remarkPlugins={[remarkGfm]}
                          >
                            {renderWikiCitationMarkers(
                              page.body,
                              wikiCitationEntries(page.metadata.citations)
                            )}
                          </ReactMarkdown>
                        </div>
                      ) : (
                        <pre className="wiki-markdown-source"><code>{page.body}</code></pre>
                      )}
                      {Object.keys(page.metadata.citations).length ? (
                        <div className="wiki-reader-references">
                          <strong>引用证据与原始资料</strong>
                          {sourceLookupError ? (
                            <p role="status">无法读取原件名称：{sourceLookupError}</p>
                          ) : null}
                          {wikiCitationEntries(page.metadata.citations).map(({ key, number, citation }) => {
                            const sourceName = sourceNames[citation.source_revision_id];
                            const spanOrdinal = Number(citation.span_id.split(":").at(-1));
                            return (
                              <div className="wiki-reader-citation" id={`wiki-citation-${key}`} key={key}>
                                <span><sup>{number}</sup> {citation.label || "引用片段"}</span>
                                <Link href={`/sources?revision=${encodeURIComponent(citation.source_revision_id)}&span=${encodeURIComponent(citation.span_id)}`}>
                                  {sourceName ?? "查看原始资料"} · 片段 {Number.isInteger(spanOrdinal) ? spanOrdinal + 1 : "原文"} →
                                </Link>
                              </div>
                            );
                          })}
                        </div>
                      ) : null}
                      {graphNeighbors(graph, selectedPath).length ? (
                        <div className="wiki-reader-related">
                          <strong>关联页面</strong>
                          <div>
                            {graphNeighbors(graph, selectedPath).map((node) => (
                              <button key={node.path} onClick={() => void selectPage(node.path)}
                                type="button">{node.title}</button>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </aside>
              ) : null}
            </div>
          ) : (
            <EmptyState title="还没有知识页面"
              description="导入第一份原件，系统处理完成后会自动生成 Wiki 页面。" />
          )}
        </section>
      ) : null}
    </>
  );
}
