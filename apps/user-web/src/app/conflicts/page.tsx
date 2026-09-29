"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type Conflict,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback } from "react";

export default function ConflictsPage() {
  const load = useCallback(() => api.listConflicts(), []);
  const { data, error, loading, reload } = useApiResource(load);
  return (
    <>
      <PageHeader
        description="Review pending Wiki pages that represent unresolved knowledge."
        eyebrow="Trust and review"
        title="Conflicts"
      />
      <Section subtitle="Pending pages returned by the repository" title="Open conflicts">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState title="No open conflicts" description="There are no pending Wiki pages." />
        ) : null}
        {data?.length ? (
          <DataTable<Conflict>
            columns={[
              { key: "title", label: "Title" },
              { key: "path", label: "Path" },
              { key: "sources", label: "Sources", render: (value) => String((value as string[]).length) },
              { key: "updated", label: "Updated", render: (value) => new Date(String(value)).toLocaleString() },
              { key: "status", label: "Status", render: (value) => <StatusBadge tone="warning">{String(value)}</StatusBadge> }
            ]}
            rowKey="path"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
