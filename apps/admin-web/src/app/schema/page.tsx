"use client";

import { PageHeader, RequestState, Section, api, useApiResource } from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useState } from "react";

export default function SchemaPage() {
  const load = useCallback(() => api.readSchema(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!data) return;
    const fields = new FormData(event.currentTarget);
    setSaving(true);
    setSaveError(null);
    try {
      await api.updateSchema(
        data.revision,
        String(fields.get("content")),
        String(fields.get("message"))
      );
      reload();
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "Unable to update the schema.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHeader description="Inspect the active knowledge schema and repository revision." eyebrow="Data governance" title="Schema" />
      <Section subtitle={data ? `Revision ${data.revision}` : "Active AGENTS.md"} title="Knowledge schema">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {data ? (
          <form className="form-grid" key={data.revision} onSubmit={(event) => void save(event)}>
            <label className="field field-wide">
              Schema content
              <textarea defaultValue={data.content} name="content" required rows={24} />
            </label>
            <label className="field field-wide">
              Commit message
              <input minLength={3} name="message" required />
            </label>
            {saveError ? <div className="form-error" role="alert">{saveError}</div> : null}
            <div className="form-actions">
              <button className="button" disabled={saving} type="submit">
                {saving ? "Publishing…" : "Publish schema revision"}
              </button>
            </div>
          </form>
        ) : null}
      </Section>
    </>
  );
}
