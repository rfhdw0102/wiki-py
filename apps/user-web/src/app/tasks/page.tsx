"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type AgentRun,
  type FeatureFlag,
  type Source
} from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useEffect, useState } from "react";

const ACTIVE_STATUSES = new Set(["queued", "running", "pending"]);

function tone(status: string) {
  if (status === "completed") return "positive" as const;
  if (status === "failed") return "danger" as const;
  if (status === "awaiting_review") return "warning" as const;
  return "info" as const;
}

export default function TasksPage() {
  const [runs, setRuns] = useState<AgentRun[] | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [features, setFeatures] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [queueing, setQueueing] = useState(false);
  const [queueError, setQueueError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [nextRuns, nextSources, nextFeatures] = await Promise.all([
        api.listRuns(),
        api.listSources(),
        api.listFeatures()
      ]);
      setRuns(nextRuns);
      setSources(nextSources);
      setFeatures(nextFeatures);
      setError(null);
      setLastUpdated(new Date());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load agent runs.");
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!runs?.some((run) => ACTIVE_STATUSES.has(run.status))) return;
    const timer = window.setInterval(() => void load(true), 2000);
    return () => window.clearInterval(timer);
  }, [load, runs]);

  async function queueSource(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const revisionId = String(form.get("revision"));
    const operation = String(form.get("operation"));
    const policy = String(form.get("policy")) as
      | "manual_review"
      | "low_risk_auto_publish";
    setQueueing(true);
    setQueueError(null);
    try {
      if (operation === "parse") {
        await api.queueParse(revisionId);
      } else {
        await api.queueCompilation(revisionId, policy);
      }
      await load(true);
    } catch (cause) {
      setQueueError(cause instanceof Error ? cause.message : "Unable to queue compilation.");
    } finally {
      setQueueing(false);
    }
  }

  async function queueCrawl(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    setQueueing(true);
    setQueueError(null);
    try {
      await api.queueCrawl({
        url: String(fields.get("url")),
        max_depth: Number(fields.get("max_depth")),
        max_pages: Number(fields.get("max_pages"))
      });
      form.reset();
      await load(true);
    } catch (cause) {
      setQueueError(cause instanceof Error ? cause.message : "Unable to queue crawl.");
    } finally {
      setQueueing(false);
    }
  }

  const compilableSources = sources.filter((source) => source.current_revision_id);
  const crawlEnabled = features.some((flag) => flag.key === "website_crawl" && flag.enabled);
  const autoPublishEnabled = features.some(
    (flag) => flag.key === "auto_publish" && flag.enabled
  );

  return (
    <>
      <PageHeader
        description="Queue source compilation and monitor your agent runs as their status changes."
        eyebrow="Automation"
        title="Agent tasks"
      />
      <Section subtitle="Parse a revision or compile it with a configured chat model" title="Process a source">
        <form className="form-grid" onSubmit={(event) => void queueSource(event)}>
          <label className="field">
            Source revision
            <select disabled={!compilableSources.length} name="revision" required>
              {compilableSources.map((source) => (
                <option key={source.id} value={source.current_revision_id ?? ""}>
                  {source.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Operation
            <select defaultValue="compile" name="operation">
              <option value="parse">Parse source</option>
              <option value="compile">Compile to Wiki</option>
            </select>
          </label>
          <label className="field">
            Publication policy
            <select defaultValue="manual_review" name="policy">
              <option value="manual_review">Manual review</option>
              <option disabled={!autoPublishEnabled} value="low_risk_auto_publish">
                Low-risk auto publish{autoPublishEnabled ? "" : " (disabled)"}
              </option>
            </select>
          </label>
          {queueError ? <div className="form-error" role="alert">{queueError}</div> : null}
          <div className="form-actions">
            <button
              className="button"
              disabled={queueing || !compilableSources.length}
              type="submit"
            >
              {queueing ? "Queueing…" : "Queue source task"}
            </button>
            {!compilableSources.length && !loading ? (
              <span className="muted">Upload a source before compiling.</span>
            ) : null}
          </div>
        </form>
      </Section>
      <Section
        subtitle={crawlEnabled ? "Crawls respect robots.txt and backend safety limits" : "The website_crawl feature is disabled"}
        title="Crawl a website"
      >
        <form className="form-grid" onSubmit={(event) => void queueCrawl(event)}>
          <label className="field field-wide">
            Starting URL
            <input name="url" placeholder="https://docs.example.com/" required type="url" />
          </label>
          <label className="field">
            Maximum depth
            <input defaultValue="1" max="5" min="0" name="max_depth" required type="number" />
          </label>
          <label className="field">
            Maximum pages
            <input defaultValue="20" max="100" min="1" name="max_pages" required type="number" />
          </label>
          <div className="form-actions">
            <button className="button" disabled={queueing || !crawlEnabled} type="submit">
              {queueing ? "Queueing…" : "Queue crawl"}
            </button>
          </div>
        </form>
      </Section>
      <Section
        subtitle={lastUpdated ? `Polled ${lastUpdated.toLocaleTimeString()}` : "Current and recent runs"}
        title="Run queue"
      >
        <RequestState error={error} loading={loading} onRetry={() => void load()} />
        {!loading && !error && runs?.length === 0 ? (
          <EmptyState title="No agent runs" description="Queue a compilation to start the first run." />
        ) : null}
        {runs?.length ? (
          <DataTable<AgentRun>
            columns={[
              { key: "id", label: "Run", render: (value) => String(value).slice(0, 8) },
              { key: "kind", label: "Kind" },
              { key: "created_at", label: "Created", render: (value) => new Date(String(value)).toLocaleString() },
              {
                key: "status",
                label: "Status",
                render: (value) => <StatusBadge tone={tone(String(value))}>{String(value)}</StatusBadge>
              },
              { key: "error", label: "Detail", render: (value) => value ? String(value) : "—" }
            ]}
            rowKey="id"
            rows={runs}
          />
        ) : null}
      </Section>
    </>
  );
}
