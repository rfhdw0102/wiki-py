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
  type SourceRevision,
  type SourceSpan,
  useApiResource
} from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useState } from "react";

export default function SourcesPage() {
  const load = useCallback(async () => {
    const [sources, features] = await Promise.all([
      api.listSources(),
      api.listFeatures()
    ]);
    return { sources, features };
  }, []);
  const { data, error, loading, reload } = useApiResource(load);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [inspectedSource, setInspectedSource] = useState<Source | null>(null);
  const [revisions, setRevisions] = useState<SourceRevision[]>([]);
  const [spans, setSpans] = useState<SourceSpan[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const uploadEnabled =
    data?.features.some((flag) => flag.key === "source_upload" && flag.enabled) ?? false;

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const file = new FormData(form).get("file");
    if (!(file instanceof File) || file.size === 0) return;
    setUploading(true);
    setUploadError(null);
    try {
      await api.uploadSource(file);
      form.reset();
      reload();
    } catch (cause) {
      setUploadError(cause instanceof Error ? cause.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function inspect(source: Source) {
    setInspectedSource(source);
    setDetailLoading(true);
    setDetailError(null);
    setSpans([]);
    try {
      const nextRevisions = await api.listSourceRevisions(source.id);
      setRevisions(nextRevisions);
      if (nextRevisions[0]) {
        setSpans(await api.listSourceSpans(nextRevisions[0].id));
      }
    } catch (cause) {
      setRevisions([]);
      setDetailError(cause instanceof Error ? cause.message : "Unable to load source details.");
    } finally {
      setDetailLoading(false);
    }
  }

  return (
    <>
      <PageHeader
        description="Upload trusted documents and monitor the revisions available for compilation."
        eyebrow="Knowledge inputs"
        title="Sources"
      />
      <Section
        subtitle={
          uploadEnabled
            ? "PDF, DOCX, Markdown, text, and HTML are supported"
            : "Source uploads are disabled by an administrator"
        }
        title="Upload source"
      >
        <form className="form-grid" onSubmit={(event) => void upload(event)}>
          <label className="field field-wide">
            Document
            <input
              accept=".pdf,.docx,.md,.txt,.html,.htm"
              name="file"
              required
              disabled={!uploadEnabled}
              type="file"
            />
          </label>
          {uploadError ? <div className="form-error" role="alert">{uploadError}</div> : null}
          <div className="form-actions">
            <button className="button" disabled={uploading || !uploadEnabled} type="submit">
              {uploading ? "Uploading…" : "Upload"}
            </button>
          </div>
        </form>
      </Section>
      <Section subtitle="Current source records returned by the API" title="Connected sources">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.sources.length === 0 ? (
          <EmptyState title="No sources yet" description="Upload a document to create the first source." />
        ) : null}
        {data?.sources.length ? (
          <DataTable<Source>
            columns={[
              { key: "name", label: "Source" },
              { key: "kind", label: "Type" },
              {
                key: "trust_level",
                label: "Trust",
                render: (value) => <StatusBadge tone="info">Level {String(value)}</StatusBadge>
              },
              {
                key: "created_at",
                label: "Created",
                render: (value) => new Date(String(value)).toLocaleString()
              },
              {
                key: "current_revision_id",
                label: "Revision",
                render: (value) => value ? String(value).slice(0, 8) : "None"
              },
              {
                key: "id",
                label: "Details",
                render: (_, row) => (
                  <button
                    className="button button-secondary button-small"
                    onClick={() => void inspect(row)}
                    type="button"
                  >
                    Inspect
                  </button>
                )
              }
            ]}
            rowKey="id"
            rows={data.sources}
          />
        ) : null}
      </Section>
      {inspectedSource ? (
        <Section
          subtitle={`${revisions.length} revision(s); spans shown for the latest revision`}
          title={inspectedSource.name}
        >
          <RequestState error={detailError} loading={detailLoading} />
          {!detailLoading && !detailError && revisions.length === 0 ? (
            <EmptyState title="No revisions" description="This source has no stored revisions." />
          ) : null}
          {!detailLoading && !detailError && revisions.length > 0 && spans.length === 0 ? (
            <EmptyState title="No parsed spans" description="Queue parsing for this revision from Agent tasks." />
          ) : null}
          {spans.length ? (
            <div className="search-results">
              {spans.map((span) => (
                <article className="result-card" key={span.id}>
                  <h3>{span.heading ?? `Span ${span.ordinal + 1}`}</h3>
                  <p>{span.content}</p>
                  <div className="muted">{JSON.stringify(span.locator)}</div>
                </article>
              ))}
            </div>
          ) : null}
        </Section>
      ) : null}
    </>
  );
}
