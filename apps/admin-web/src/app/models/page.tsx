"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type ModelAssignment,
  type ModelKind,
  type ModelProfile,
  useApiResource
} from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useState } from "react";

export default function ModelsPage() {
  const load = useCallback(async () => {
    const [profiles, assignments] = await Promise.all([
      api.listModelProfiles(),
      api.listModelAssignments()
    ]);
    return { profiles, assignments };
  }, []);
  const { data, error, loading, reload } = useApiResource(load);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [assigning, setAssigning] = useState(false);
  const [assignmentError, setAssignmentError] = useState<string | null>(null);
  const [purpose, setPurpose] = useState("compilation");

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setCreating(true);
    setCreateError(null);
    try {
      const configText = String(form.get("config") || "{}");
      const config = JSON.parse(configText) as Record<string, unknown>;
      if (!config || Array.isArray(config) || typeof config !== "object") {
        throw new Error("Configuration must be a JSON object.");
      }
      await api.createModelProfile({
        name: String(form.get("name")),
        kind: String(form.get("kind")) as ModelKind,
        base_url: String(form.get("base_url")),
        model_name: String(form.get("model_name")),
        api_key: String(form.get("api_key")),
        enabled: true,
        config
      });
      formElement.reset();
      reload();
    } catch (cause) {
      setCreateError(cause instanceof Error ? cause.message : "Unable to create model profile.");
    } finally {
      setCreating(false);
    }
  }

  async function assign(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    setAssigning(true);
    setAssignmentError(null);
    try {
      await api.updateModelAssignment(
        String(fields.get("purpose")),
        String(fields.get("model_profile_id"))
      );
      reload();
    } catch (cause) {
      setAssignmentError(cause instanceof Error ? cause.message : "Unable to update assignment.");
    } finally {
      setAssigning(false);
    }
  }

  const requiredKind: ModelKind =
    purpose === "embedding" ? "embedding" : purpose === "reranker" ? "reranker" : "chat";
  const compatibleProfiles =
    data?.profiles.filter((profile) => profile.enabled && profile.kind === requiredKind) ?? [];

  return (
    <>
      <PageHeader
        description="Register provider endpoints and credentials used by agent workloads."
        eyebrow="AI infrastructure"
        title="Model profiles"
      />
      <Section subtitle="API keys are encrypted by the backend and are never returned" title="Create profile">
        <form className="form-grid" onSubmit={(event) => void create(event)}>
          <label className="field">Name<input name="name" required /></label>
          <label className="field">
            Kind
            <select defaultValue="chat" name="kind">
              <option value="chat">Chat</option>
              <option value="embedding">Embedding</option>
              <option value="reranker">Reranker</option>
            </select>
          </label>
          <label className="field">Base URL<input name="base_url" placeholder="https://api.example.com/v1" required type="url" /></label>
          <label className="field">Model name<input name="model_name" required /></label>
          <label className="field">API key<input autoComplete="off" name="api_key" required type="password" /></label>
          <label className="field">Configuration JSON<textarea defaultValue="{}" name="config" rows={3} /></label>
          {createError ? <div className="form-error" role="alert">{createError}</div> : null}
          <div className="form-actions">
            <button className="button" disabled={creating} type="submit">
              {creating ? "Creating…" : "Create profile"}
            </button>
          </div>
        </form>
      </Section>
      <Section subtitle="Configured model endpoints" title="Model registry">
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.profiles.length === 0 ? (
          <EmptyState title="No model profiles" description="Create a profile to enable model-backed flows." />
        ) : null}
        {data?.profiles.length ? (
          <DataTable<ModelProfile>
            columns={[
              { key: "name", label: "Name" },
              { key: "kind", label: "Kind" },
              { key: "model_name", label: "Model" },
              { key: "base_url", label: "Endpoint" },
              { key: "enabled", label: "Status", render: (value) => <StatusBadge tone={value ? "positive" : "neutral"}>{value ? "Enabled" : "Disabled"}</StatusBadge> }
            ]}
            rowKey="id"
            rows={data.profiles}
          />
        ) : null}
      </Section>
      <Section subtitle="Route each workload purpose to a compatible enabled profile" title="Model assignments">
        {data?.profiles.length ? (
          <form className="form-grid" onSubmit={(event) => void assign(event)}>
            <label className="field">
              Purpose
              <select
                name="purpose"
                onChange={(event) => setPurpose(event.target.value)}
                value={purpose}
              >
                <option value="planning">Planning (chat)</option>
                <option value="compilation">Compilation (chat)</option>
                <option value="answer">Answers (chat)</option>
                <option value="embedding">Embedding</option>
                <option value="reranker">Reranking</option>
              </select>
            </label>
            <label className="field">
              Profile
              <select name="model_profile_id" required>
                {compatibleProfiles.map((profile) => (
                    <option key={profile.id} value={profile.id}>
                      {profile.name} ({profile.kind})
                    </option>
                ))}
              </select>
            </label>
            {assignmentError ? <div className="form-error" role="alert">{assignmentError}</div> : null}
            <div className="form-actions">
              <button
                className="button"
                disabled={assigning || compatibleProfiles.length === 0}
                type="submit"
              >
                {assigning ? "Saving…" : "Save assignment"}
              </button>
              {compatibleProfiles.length === 0 ? (
                <span className="muted">Create an enabled {requiredKind} profile first.</span>
              ) : null}
            </div>
          </form>
        ) : null}
        {data?.assignments.length ? (
          <DataTable<ModelAssignment>
            columns={[
              { key: "purpose", label: "Purpose" },
              {
                key: "model_profile_id",
                label: "Profile",
                render: (value) =>
                  data.profiles.find((profile) => profile.id === value)?.name ??
                  String(value)
              }
            ]}
            rowKey="purpose"
            rows={data.assignments}
          />
        ) : !loading && !error ? (
          <EmptyState title="No assignments" description="Assign a profile to enable model-backed workloads." />
        ) : null}
      </Section>
    </>
  );
}
