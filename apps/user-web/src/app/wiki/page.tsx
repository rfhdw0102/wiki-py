"use client";

import {
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type WikiPage,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback, useState } from "react";

export default function WikiBrowserPage() {
  const load = useCallback(() => api.listWikiPages(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [page, setPage] = useState<WikiPage | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [pageLoading, setPageLoading] = useState(false);

  async function selectPage(path: string) {
    setSelectedPath(path);
    setPageLoading(true);
    setPageError(null);
    try {
      setPage(await api.readWikiPage(path));
    } catch (cause) {
      setPage(null);
      setPageError(cause instanceof Error ? cause.message : "Unable to load the page.");
    } finally {
      setPageLoading(false);
    }
  }

  return (
    <>
      <PageHeader
        description="Browse the published, source-backed Wiki at its current revision."
        eyebrow="Curated knowledge"
        title="Wiki"
      />
      <Section
        subtitle={data ? `${data.pages.length} pages · revision ${data.revision.slice(0, 10)}` : "Published pages"}
        title="Page browser"
      >
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.pages.length === 0 ? (
          <EmptyState title="No Wiki pages" description="Compiled and published pages will appear here." />
        ) : null}
        {data?.pages.length ? (
          <div className="wiki-browser">
            <ul className="page-list">
              {data.pages.map((path) => (
                <li key={path}>
                  <button
                    aria-current={selectedPath === path}
                    onClick={() => void selectPage(path)}
                    type="button"
                  >
                    {path}
                  </button>
                </li>
              ))}
            </ul>
            <article className="wiki-document">
              <RequestState error={pageError} loading={pageLoading} />
              {!page && !pageLoading && !pageError ? (
                <EmptyState title="Select a page" description="Choose a Wiki path to read its content." />
              ) : null}
              {page ? (
                <>
                  <StatusBadge tone="positive">{page.metadata.status}</StatusBadge>
                  <h2>{page.metadata.title}</h2>
                  <div className="muted">
                    {page.metadata.type} · updated {new Date(page.metadata.updated).toLocaleString()} ·{" "}
                    {Object.keys(page.metadata.citations).length} citations
                  </div>
                  <div className="wiki-body">{page.body}</div>
                </>
              ) : null}
            </article>
          </div>
        ) : null}
      </Section>
    </>
  );
}
