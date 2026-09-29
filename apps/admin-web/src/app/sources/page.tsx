"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type Source,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback, useState } from "react";

export default function AdminSourcesPage() {
  const load = useCallback(() => api.listSources(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [updating, setUpdating] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);

  async function updateTrust(source: Source, trustLevel: number) {
    setUpdating(source.id);
    setUpdateError(null);
    try {
      await api.updateSourceTrust(source.id, trustLevel);
      reload();
    } catch (cause) {
      setUpdateError(cause instanceof Error ? cause.message : "Unable to update source trust.");
    } finally {
      setUpdating(null);
    }
  }

  return (
    <>
      <PageHeader
        description="Review ingested sources and set the trust level used by publication policy."
        eyebrow="Knowledge governance"
        title="Sources"
      />
      <Section subtitle="Trust levels range from untrusted (0) to approved authority (3)" title="Source trust">
        {updateError ? <div className="request-state request-error" role="alert">{updateError}</div> : null}
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState title="No sources" description="Sources uploaded by users will appear here." />
        ) : null}
        {data?.length ? (
          <DataTable<Source>
            columns={[
              { key: "name", label: "Source" },
              { key: "kind", label: "Kind" },
              {
                key: "trust_level",
                label: "Current trust",
                render: (value) => <StatusBadge tone="info">Level {String(value)}</StatusBadge>
              },
              {
                key: "id",
                label: "Set trust",
                render: (_, row) => (
                  <select
                    aria-label={`Trust level for ${row.name}`}
                    defaultValue={String(row.trust_level)}
                    disabled={updating === row.id}
                    onChange={(event) => void updateTrust(row, Number(event.target.value))}
                  >
                    <option value="0">0 — Untrusted</option>
                    <option value="1">1 — Unverified</option>
                    <option value="2">2 — Trusted</option>
                    <option value="3">3 — Authority</option>
                  </select>
                )
              }
            ]}
            rowKey="id"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
