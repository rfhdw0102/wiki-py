"use client";

import { DataTable, EmptyState, PageHeader, RequestState, Section, StatusBadge, api, type AdminJob, useApiResource } from "@wiki-agent/web-ui";
import { useCallback, useState } from "react";

export default function JobsPage() {
  const load = useCallback(() => api.listAdminJobs(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildResult, setRebuildResult] = useState<string | null>(null);
  const [rebuildError, setRebuildError] = useState<string | null>(null);

  async function rebuildIndex() {
    setRebuilding(true);
    setRebuildError(null);
    setRebuildResult(null);
    try {
      const result = await api.rebuildWikiIndex();
      setRebuildResult(`Rebuilt projection for commit ${result.commit}.`);
    } catch (cause) {
      setRebuildError(cause instanceof Error ? cause.message : "Unable to rebuild the index.");
    } finally {
      setRebuilding(false);
    }
  }

  return (
    <>
      <PageHeader description="Observe queued workloads and execution outcomes." eyebrow="Platform operations" title="Jobs" />
      <Section
        action={
          <button
            className="button button-secondary"
            disabled={rebuilding}
            onClick={() => void rebuildIndex()}
            type="button"
          >
            {rebuilding ? "Rebuilding…" : "Rebuild Wiki index"}
          </button>
        }
        subtitle="Latest agent runs across all users"
        title="Job queue"
      >
        {rebuildError ? <div className="request-state request-error" role="alert">{rebuildError}</div> : null}
        {rebuildResult ? <div className="request-state" role="status">{rebuildResult}</div> : null}
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? <EmptyState title="No jobs" description="No agent jobs have been queued." /> : null}
        {data?.length ? (
          <DataTable<AdminJob>
            columns={[
              { key: "id", label: "Job", render: (value) => String(value).slice(0, 8) },
              { key: "kind", label: "Kind" },
              { key: "created_by_id", label: "Owner", render: (value) => String(value).slice(0, 8) },
              { key: "updated_at", label: "Updated", render: (value) => new Date(String(value)).toLocaleString() },
              { key: "status", label: "Status", render: (value) => <StatusBadge tone={value === "completed" ? "positive" : value === "failed" ? "danger" : "info"}>{String(value)}</StatusBadge> },
              { key: "error", label: "Error", render: (value) => value ? String(value) : "—" }
            ]}
            rowKey="id"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
