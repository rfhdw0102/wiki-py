"use client";

import Link from "next/link";
import { DataTable, EmptyState, PageHeader, RequestState, Section, StatCard, StatusBadge, api, type AdminJob, useApiResource } from "@wiki-agent/web-ui";
import { useCallback } from "react";

export default function AdminOverviewPage() {
  const load = useCallback(async () => {
    const [users, models, features, jobs] = await Promise.all([
      api.listUsers(),
      api.listModelProfiles(),
      api.listFeatureFlags(),
      api.listAdminJobs()
    ]);
    return { users, models, features, jobs };
  }, []);
  const { data, error, loading, reload } = useApiResource(load);

  return (
    <>
      <PageHeader description="Monitor configured resources and recent agent workloads." eyebrow="Administration" title="System overview" />
      <RequestState error={error} loading={loading} onRetry={reload} />
      {data ? (
        <>
          <div className="stats-grid">
            <StatCard label="Active users" value={String(data.users.filter((user) => user.is_active).length)} detail={`${data.users.length} total accounts`} tone="accent" />
            <StatCard label="Models" value={String(data.models.length)} detail={`${data.models.filter((model) => model.enabled).length} enabled`} />
            <StatCard label="Enabled flags" value={String(data.features.filter((flag) => flag.enabled).length)} detail={`${data.features.length} configured`} />
            <StatCard label="Active jobs" value={String(data.jobs.filter((job) => ["queued", "running", "pending"].includes(job.status)).length)} detail={`${data.jobs.length} recent jobs`} />
          </div>
          <Section action={<Link className="text-link" href="/jobs">All jobs</Link>} subtitle="Latest workloads across users" title="Job activity">
            {data.jobs.length === 0 ? <EmptyState title="No jobs" description="No agent workloads have been queued." /> : (
              <DataTable<AdminJob>
                columns={[
                  { key: "id", label: "Job", render: (value) => String(value).slice(0, 8) },
                  { key: "kind", label: "Kind" },
                  { key: "updated_at", label: "Updated", render: (value) => new Date(String(value)).toLocaleString() },
                  { key: "status", label: "Status", render: (value) => <StatusBadge tone={value === "completed" ? "positive" : value === "failed" ? "danger" : "info"}>{String(value)}</StatusBadge> }
                ]}
                rowKey="id"
                rows={data.jobs.slice(0, 6)}
              />
            )}
          </Section>
        </>
      ) : null}
    </>
  );
}
