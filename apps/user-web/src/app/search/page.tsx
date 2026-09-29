"use client";

import {
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type WikiQuestionResponse,
  type WikiSearchResponse,
  useApiResource
} from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useState } from "react";

export default function SearchPage() {
  const loadFeatures = useCallback(() => api.listFeatures(), []);
  const {
    data: features,
    error: featureError,
    loading: featuresLoading,
    reload: reloadFeatures
  } = useApiResource(loadFeatures);
  const [result, setResult] = useState<WikiSearchResponse | null>(null);
  const [answer, setAnswer] = useState<WikiQuestionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const questionsEnabled =
    features?.some((flag) => flag.key === "wiki_questions" && flag.enabled) ?? false;

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = String(new FormData(event.currentTarget).get("query")).trim();
    const mode = String(new FormData(event.currentTarget).get("mode"));
    if (!query) return;
    setLoading(true);
    setError(null);
    try {
      if (mode === "question") {
        setAnswer(await api.askWiki(query));
        setResult(null);
      } else {
        setResult(await api.searchWiki(query));
        setAnswer(null);
      }
    } catch (cause) {
      setResult(null);
      setError(cause instanceof Error ? cause.message : "Search failed.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      <PageHeader
        description="Search across Wiki pages and, when enabled, underlying source content."
        eyebrow="Workspace discovery"
        title="Search"
      />
      <RequestState
        error={featureError}
        loading={featuresLoading}
        onRetry={reloadFeatures}
      />
      <form className="search-box" onSubmit={(event) => void search(event)}>
        <input
          aria-label="Search the workspace"
          disabled={!questionsEnabled}
          name="query"
          placeholder={
            questionsEnabled
              ? "Search pages and sources"
              : "Wiki search is disabled by an administrator"
          }
          required
        />
        <select aria-label="Query mode" disabled={!questionsEnabled} name="mode">
          <option value="search">Search</option>
          <option value="question">Ask a question</option>
        </select>
        <button className="button" disabled={loading || !questionsEnabled} type="submit">
          {loading ? "Searching…" : "Search"}
        </button>
      </form>
      <Section
        subtitle={
          result
            ? `${result.hits.length} matches · ${result.layer} layer`
            : answer
              ? `${answer.evidence.length} evidence item(s) · ${answer.layer} layer`
              : "Enter a query above"
        }
        title={answer ? "Answer" : "Results"}
      >
        <RequestState error={error} loading={loading} />
        {result && result.hits.length === 0 ? (
          <EmptyState title="No matches" description="Try a broader query or add more source material." />
        ) : null}
        {result?.hits.length ? (
          <div className="search-results">
            {result.hits.map((hit) => (
              <article className="result-card" key={hit.document_id}>
                <h3>{typeof hit.metadata.title === "string" ? hit.metadata.title : hit.document_id}</h3>
                <p>{hit.content}</p>
                <div className="result-meta">
                  <StatusBadge tone="info">{hit.channels.join(", ")}</StatusBadge>
                  <StatusBadge>{hit.score.toFixed(3)}</StatusBadge>
                </div>
              </article>
            ))}
          </div>
        ) : null}
        {answer ? (
          <article className="wiki-document">
            <StatusBadge tone={answer.insufficient_evidence ? "warning" : "positive"}>
              {answer.insufficient_evidence ? "Insufficient evidence" : "Evidence-backed"}
            </StatusBadge>
            <div className="wiki-body">{answer.answer}</div>
            {answer.uncertainty ? <p className="muted">{answer.uncertainty}</p> : null}
            <div className="result-meta">
              <StatusBadge tone="info">{answer.channels.join(", ") || "No channels"}</StatusBadge>
              <StatusBadge>{answer.citations.length} citations</StatusBadge>
            </div>
          </article>
        ) : null}
      </Section>
    </>
  );
}
