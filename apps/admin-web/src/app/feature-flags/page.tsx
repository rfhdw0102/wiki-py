"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type FeatureFlag,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback, useState } from "react";

export default function FeatureFlagsPage() {
  const load = useCallback(() => api.listFeatureFlags(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [updating, setUpdating] = useState<string | null>(null);
  const [updateError, setUpdateError] = useState<string | null>(null);

  async function toggle(flag: FeatureFlag) {
    setUpdating(flag.key);
    setUpdateError(null);
    try {
      await api.updateFeatureFlag(flag.key, !flag.enabled, flag.config);
      reload();
    } catch (cause) {
      setUpdateError(cause instanceof Error ? cause.message : "Unable to update feature.");
    } finally {
      setUpdating(null);
    }
  }

  return (
    <>
      <PageHeader
        description="Inspect and enable or disable backend feature gates."
        eyebrow="Release controls"
        title="Feature flags"
      />
      <Section subtitle="Updates preserve each flag's current configuration" title="Flags">
        {updateError ? <div className="request-state request-error" role="alert">{updateError}</div> : null}
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState title="No flags" description="No feature flags are configured." />
        ) : null}
        {data?.length ? (
          <DataTable<FeatureFlag>
            columns={[
              { key: "key", label: "Flag" },
              { key: "config", label: "Configuration", render: (value) => <code>{JSON.stringify(value)}</code> },
              { key: "enabled", label: "Status", render: (value) => <StatusBadge tone={value ? "positive" : "neutral"}>{value ? "Enabled" : "Disabled"}</StatusBadge> },
              {
                key: "enabled",
                label: "Action",
                render: (_, row) => (
                  <button
                    className="button button-secondary button-small"
                    disabled={updating === row.key}
                    onClick={() => void toggle(row)}
                    type="button"
                  >
                    {updating === row.key ? "Saving…" : row.enabled ? "Disable" : "Enable"}
                  </button>
                )
              }
            ]}
            rowKey="key"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
