import type { AgentRun, Conflict, Draft } from "@wiki-agent/web-ui";

const RISK_LABELS: Record<string, string> = {
  unresolved_conflicts: "存在待处理冲突",
  too_many_changed_pages: "改动页面过多",
  change_ratio_too_large: "现有内容改写较多",
  too_many_new_claims: "新增结论过多",
  source_trust_too_low: "来源可信度不足",
  citations_incomplete: "来源引用不完整",
  contains_deletions: "包含页面删除",
  contains_renames: "包含页面重命名",
  contains_entity_merges: "包含实体合并",
  schema_invalid: "页面格式校验未通过",
  links_invalid: "页面链接校验未通过",
  verifier_failed: "独立校验未通过",
  workflow_degraded: "编译流程降级",
  stale_base_commit: "Wiki 版本已变化"
};

export function draftCompilationRun(draft: Draft, runs: AgentRun[]): AgentRun | undefined {
  return runs.find((item) =>
    item.kind === "compile_source" && item.result_json?.draft_id === draft.id
  );
}

export function reviewReasons(draft: Draft, runs: AgentRun[]): string[] {
  const run = draftCompilationRun(draft, runs);
  const violations = run?.result_json?.risk_violations;
  const reasons = Array.isArray(violations)
    ? violations.filter((value): value is string => typeof value === "string")
      .map((value) => RISK_LABELS[value] ?? value)
    : [];
  if (Object.keys(draft.files).some((path) => path.startsWith("pending/")) &&
      !reasons.includes(RISK_LABELS.unresolved_conflicts)) {
    reasons.push(RISK_LABELS.unresolved_conflicts);
  }
  return reasons;
}

export type ConflictGroup = {
  id: string;
  title: string;
  conflicts: Conflict[];
  sources: string[];
  updated: string;
};

function compareUpdated(a: string, b: string): number {
  const difference = Date.parse(a) - Date.parse(b);
  return Number.isNaN(difference) ? a.localeCompare(b) : difference;
}

export function groupConflicts(conflicts: Conflict[]): ConflictGroup[] {
  const groups = new Map<string, ConflictGroup>();
  for (const conflict of conflicts) {
    const id = conflict.batch_id ? `batch:${conflict.batch_id}` : `page:${conflict.path}`;
    const group = groups.get(id);
    if (!group) {
      groups.set(id, {
        id,
        title: conflict.batch_id ? conflict.batch_title || "批量导入的待处理冲突" : conflict.title,
        conflicts: [conflict],
        sources: [...new Set(conflict.sources)],
        updated: conflict.updated
      });
    } else {
      group.conflicts.push(conflict);
      group.sources = [...new Set([...group.sources, ...conflict.sources])];
      if (conflict.batch_title) group.title = conflict.batch_title;
      if (compareUpdated(conflict.updated, group.updated) > 0) group.updated = conflict.updated;
    }
  }
  return [...groups.values()].sort((a, b) => compareUpdated(b.updated, a.updated));
}

export function openDrafts(drafts: Draft[]): Draft[] {
  return drafts.filter((draft) => draft.status === "open");
}

export function reviewExplanationError(decision: "publish" | "reject", explanation: string): string | null {
  if (decision === "reject" && explanation.trim().length < 3) {
    return "请填写至少 3 个字符的拒绝原因。";
  }
  return null;
}

export function reviewFailureMessage(message: string): string {
  const messages: Record<string, string> = {
    "Source is still being processed": "原件仍在处理中，暂时不能删除。请等待处理结束后再拒绝发布。",
    "Source is referenced by Wiki pages; remove the references first":
      "该原件已被发布的 Wiki 页面引用，不能删除。此次拒绝未执行，草稿和原件均保留。",
    "Source is referenced by a draft; remove the draft first":
      "其他草稿也在使用该原件，不能删除。此次拒绝未执行，草稿和原件均保留。",
    "Source deleted, but object cleanup failed":
      "草稿、原件记录及处理记录已删除，但原件文件清理失败。请查看运行日志。",
    "Invalid source object path": "原件存储路径异常，已停止删除。草稿和原件均保留。"
  };
  return messages[message] ?? message;
}
