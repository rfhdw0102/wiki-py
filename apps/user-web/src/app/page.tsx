"use client";

import Link from "next/link";
import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatCard,
  StatusBadge,
  api,
  type Source,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback } from "react";

export default function DashboardPage() {
  const load = useCallback(async () => {
    const [sources, pages, runs, drafts] = await Promise.all([
      api.listSources(),
      api.listWikiPages(),
      api.listRuns(),
      api.listDrafts()
    ]);
    return { sources, pages, runs, drafts };
  }, []);
  const { data, error, loading, reload } = useApiResource(load);
  const activeRuns = data?.runs.filter((run) => ["queued", "running", "pending"].includes(run.status)).length ?? 0;
  const openDrafts = data?.drafts.filter((draft) => draft.status === "open").length ?? 0;

  return (
    <>
      <PageHeader
        action={<Link className="button" href="/search">Search the Wiki</Link>}
        description="Bring trusted sources together, compile them, and keep every result traceable."
        eyebrow="Knowledge workspace"
        title="Workspace overview"
      />
      <RequestState error={error} loading={loading} onRetry={reload} />
      {data ? (
        <>
          <div className="stats-grid">
            <StatCard label="Wiki pages" value={String(data.pages.pages.length)} detail={`Revision ${data.pages.revision.slice(0, 8)}`} tone="accent" />
            <StatCard label="Sources" value={String(data.sources.length)} detail="Available to agents" />
            <StatCard label="Active runs" value={String(activeRuns)} detail={`${data.runs.length} total runs`} />
            <StatCard label="Open drafts" value={String(openDrafts)} detail={`${data.drafts.length} total drafts`} />
          </div>
          <Section
            action={<Link className="text-link" href="/sources">View all</Link>}
            subtitle="Most recently added source records"
            title="Sources"
          >
            {data.sources.length === 0 ? (
              <EmptyState title="No sources" description="Upload a document to begin." />
            ) : (
              <DataTable<Source>
                columns={[
                  { key: "name", label: "Source" },
                  { key: "kind", label: "Type" },
                  { key: "created_at", label: "Created", render: (value) => new Date(String(value)).toLocaleString() },
                  { key: "current_revision_id", label: "Status", render: (value) => <StatusBadge tone={value ? "positive" : "warning"}>{value ? "Revision ready" : "No revision"}</StatusBadge> }
                ]}
                rowKey="id"
                rows={data.sources.slice(0, 5)}
              />
            )}
          </Section>
        </>
      ) : null}
    </>
  );
}
