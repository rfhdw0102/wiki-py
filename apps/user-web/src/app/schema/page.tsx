"use client";

import {
  RequestState,
  api,
  useApiResource
} from "@wiki-agent/web-ui";
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
      await api.updateSchema(data.revision, String(fields.get("content")));
      reload();
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "无法更新知识规范。");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="panel schema-editor">
      <RequestState error={error} loading={loading} onRetry={reload} />
      {data ? (
        <form className="form-grid" key={data.revision} onSubmit={(event) => void save(event)}>
          <label className="field field-wide">
            规范内容
            <textarea defaultValue={data.content} name="content" required rows={24} />
          </label>
          {saveError ? <div className="form-error" role="alert">{saveError}</div> : null}
          <div className="form-actions">
            <button className="button" disabled={saving} type="submit">
              {saving ? "正在保存…" : "保存规则"}
            </button>
          </div>
        </form>
      ) : null}
    </section>
  );
}
