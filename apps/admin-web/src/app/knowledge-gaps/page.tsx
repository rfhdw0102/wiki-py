"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type KnowledgeGap,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback } from "react";

export default function KnowledgeGapsPage() {
  const load = useCallback(() => api.listKnowledgeGaps(), []);
  const { data, error, loading, reload } = useApiResource(load);

  return (
    <>
      <PageHeader
        description="Find repeated questions that required raw-source fallback or returned no evidence."
        eyebrow="Knowledge operations"
        title="Knowledge gaps"
      />
      <Section subtitle="Ordered by frequency and most recent occurrence" title="Low-coverage queries">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState
            title="No observed gaps"
            description="Fallback and zero-result queries will be summarized here."
          />
        ) : null}
        {data?.length ? (
          <DataTable<KnowledgeGap>
            columns={[
              { key: "query", label: "Query" },
              {
                key: "occurrences",
                label: "Occurrences",
                render: (value) => <StatusBadge tone="warning">{String(value)}</StatusBadge>
              },
              { key: "best_hit_count", label: "Best raw hits" },
              {
                key: "last_seen",
                label: "Last seen",
                render: (value) => new Date(String(value)).toLocaleString()
              }
            ]}
            rowKey="query"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
