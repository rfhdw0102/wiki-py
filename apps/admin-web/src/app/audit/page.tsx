"use client";

import { DataTable, EmptyState, PageHeader, RequestState, Section, api, type AuditEntry, useApiResource } from "@wiki-agent/web-ui";
import { useCallback } from "react";

export default function AuditPage() {
  const load = useCallback(() => api.listAuditEntries(), []);
  const { data, error, loading, reload } = useApiResource(load);
  return (
    <>
      <PageHeader description="Review records of privileged actions and policy decisions." eyebrow="Compliance" title="Audit log" />
      <Section subtitle="Latest control-plane and access events" title="Events">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? <EmptyState title="No events" description="No audit events have been recorded." /> : null}
        {data?.length ? (
          <DataTable<AuditEntry>
            columns={[
              { key: "action", label: "Action" },
              { key: "resource_type", label: "Resource" },
              { key: "resource_id", label: "Resource ID", render: (value) => value ? String(value).slice(0, 12) : "—" },
              { key: "actor_id", label: "Actor", render: (value) => value ? String(value).slice(0, 8) : "System" },
              { key: "created_at", label: "Created", render: (value) => new Date(String(value)).toLocaleString() }
            ]}
            rowKey="id"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
