import type { Source } from "@wiki-agent/web-ui";

export function sourceSelectionState(sources: Source[], selected: ReadonlySet<string>) {
  const count = sources.filter((source) => selected.has(source.id)).length;
  return {
    count,
    all: sources.length > 0 && count === sources.length,
    mixed: count > 0 && count < sources.length
  };
}

export function toggleVisibleSources(sources: Source[], selected: ReadonlySet<string>): Set<string> {
  const next = new Set(selected);
  const { all } = sourceSelectionState(sources, selected);
  for (const source of sources) {
    if (all) next.delete(source.id);
    else next.add(source.id);
  }
  return next;
}

export function selectedSources(sources: Source[], selected: ReadonlySet<string>): Source[] {
  return sources.filter((source) => selected.has(source.id));
}

export function sourceCompilationPlan(sources: Source[], eligible: (source: Source) => boolean) {
  const ready: Source[] = [];
  const skipped: Source[] = [];
  for (const source of sources) (eligible(source) ? ready : skipped).push(source);
  return { ready, skipped };
}

export function validateSourceName(value: string): string {
  if (/[/\\\r\n\v\f\x1c-\x1e\u0085\u2028\u2029]/u.test(value)) {
    throw new Error("名称不能包含换行或路径分隔符（/、\\）。");
  }
  const name = value.trim();
  if (!name) throw new Error("名称不能为空。");
  if (Array.from(name).length > 512) throw new Error("名称不能超过 512 个字符。");
  return name;
}

export type SourceBatchResult = {
  succeeded: Source[];
  failed: { source: Source; message: string }[];
};

export async function runSourceBatch(
  sources: Source[],
  perform: (source: Source) => Promise<void>,
  failureMessage: (cause: unknown) => string,
  onProgress: (source: Source, index: number) => void,
  pause: () => Promise<void> = () => new Promise((resolve) => setTimeout(resolve, 250))
): Promise<SourceBatchResult> {
  const result: SourceBatchResult = { succeeded: [], failed: [] };
  for (const [index, source] of sources.entries()) {
    if (index) await pause();
    onProgress(source, index);
    try {
      await perform(source);
      result.succeeded.push(source);
    } catch (cause) {
      result.failed.push({ source, message: failureMessage(cause) });
    }
  }
  return result;
}
