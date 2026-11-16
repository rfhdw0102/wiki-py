import type { WikiCitation } from "@wiki-agent/web-ui";

export interface WikiCitationEntry {
  key: string;
  number: number;
  citation: WikiCitation;
}

export function wikiCitationEntries(
  citations: Record<string, WikiCitation>
): WikiCitationEntry[] {
  return Object.entries(citations).map(([key, citation], index) => ({
    key,
    number: index + 1,
    citation
  }));
}

export function renderWikiCitationMarkers(
  body: string,
  entries: readonly WikiCitationEntry[]
): string {
  const numbers = new Map(entries.map(({ key, number }) => [key, number]));
  return body.replace(/\[\^([a-zA-Z0-9_-]+)\]/g, (marker, key: string) => {
    const number = numbers.get(key);
    return number === undefined ? marker : `[${number}](#wiki-citation-${key})`;
  });
}
