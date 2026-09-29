"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type Draft,
  useApiResource
} from "@wiki-agent/web-ui";
import { useCallback, useState } from "react";

export default function DraftsPage() {
  const load = useCallback(() => api.listDrafts(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [publishing, setPublishing] = useState<string | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);

  async function publish(draft: Draft) {
    const message = messages[draft.id]?.trim();
    if (!message) {
      setPublishError("Enter a publish message with at least 3 characters.");
      return;
    }
    setPublishing(draft.id);
    setPublishError(null);
    try {
      await api.publishDraft(draft.id, message);
      reload();
    } catch (cause) {
      setPublishError(cause instanceof Error ? cause.message : "Publish failed.");
    } finally {
      setPublishing(null);
    }
  }

  return (
    <>
      <PageHeader
        description="Review your generated or manually created changes and publish open drafts."
        eyebrow="Review workflow"
        title="Drafts"
      />
      <Section subtitle="Only the draft creator can publish an open draft" title="My drafts">
        {publishError ? <div className="request-state request-error" role="alert">{publishError}</div> : null}
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState title="No drafts" description="Compilation results awaiting review will appear here." />
        ) : null}
        {data?.length ? (
          <DataTable<Draft>
            columns={[
              { key: "title", label: "Draft" },
              { key: "files", label: "Files", render: (value) => String(Object.keys(value as Draft["files"]).length) },
              { key: "created_at", label: "Created", render: (value) => new Date(String(value)).toLocaleString() },
              {
                key: "status",
                label: "Status",
                render: (value) => (
                  <StatusBadge tone={value === "published" ? "positive" : value === "open" ? "warning" : "neutral"}>
                    {String(value)}
                  </StatusBadge>
                )
              },
              {
                key: "id",
                label: "Publish",
                render: (_, row) =>
                  row.status === "open" ? (
                    <div className="inline-form">
                      <input
                        aria-label={`Publish message for ${row.title}`}
                        onChange={(event) =>
                          setMessages((current) => ({ ...current, [row.id]: event.target.value }))
                        }
                        placeholder="Publish message"
                        value={messages[row.id] ?? ""}
                      />
                      <button
                        className="button button-small"
                        disabled={publishing === row.id}
                        onClick={() => void publish(row)}
                        type="button"
                      >
                        {publishing === row.id ? "Publishing…" : "Publish"}
                      </button>
                    </div>
                  ) : row.published_commit?.slice(0, 10) ?? "—"
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
