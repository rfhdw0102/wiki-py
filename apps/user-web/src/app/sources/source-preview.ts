import type { SourceSpan } from "@wiki-agent/web-ui";

export function sourceMarkdown(preview: {
  text: string | null;
  spans: SourceSpan[];
}): string {
  if (!preview.spans.length) return preview.text ?? "";
  return preview.spans.map((span) => span.content).join("\n\n");
}
