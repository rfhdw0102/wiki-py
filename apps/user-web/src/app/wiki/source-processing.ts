import type { AgentRun, Draft, Source, SourceBatchIngest, SourceIngest, StatusTone } from "@wiki-agent/web-ui";

export type SourceProcessingStatus = {
  label: string;
  tone: StatusTone;
  detail: string;
  action?: { href: "/models" | "/conflicts"; label: string };
};

export function importFailureMessage(error: string | null): string {
  if (error?.includes("do not have a bidirectional link")) {
    return "生成的知识页面缺少相互关联，未通过 Wiki 校验。原件已保存；系统已增加自动补齐关联，可重试处理。";
  }
  if (error?.includes("links to missing page") || error?.includes("references missing source page")) {
    return "生成的知识页面引用了不存在的页面，未通过 Wiki 校验。原件已保存，请重试处理。";
  }
  if (error?.includes("references non-source page")) {
    return "生成的知识页面使用了错误的来源类型，未通过 Wiki 校验。原件已保存，请重试处理。";
  }
  if (error?.includes("No compilation model is assigned")) {
    return "当前账号尚未分配知识编译模型，请先在模型设置中完成配置。资料已保存，但尚未编译。";
  }
  if (error?.includes("Queue submission failed")) {
    return "后台任务提交失败，请检查 Redis 和工作进程是否正常运行。资料已保存，可稍后重试。";
  }
  if (error?.includes("document-parsing extra") || error?.includes("No module named")) {
    return "文档解析环境缺少必要依赖，请检查解析工作进程后重试。";
  }
  if (error?.includes("OpenAITimeoutError")) {
    return "知识处理中的模型调用超时，多次尝试后仍未返回结果。原件已保存且尚未发布；可稍后重试，或检查模型接口的响应速度。";
  }
  if (error?.includes("CompilationOutputError")) {
    if (error.includes("Concept pages require at least two independent sources")) {
      return "模型生成的概念页未声明至少两份独立来源，未发布。这不代表只上传了一份资料；每个概念都必须关联共同支持它的来源摘要。原件已保存，可重试处理。";
    }
    if (error.includes("Pending pages cannot have published status")) {
      return "模型将待处理的冲突页标记为已发布，未通过校验。冲突必须保留为待审核状态。";
    }
    if (error.includes("unsafe HTML or a dangerous link scheme")) {
      return "模型生成的页面包含不安全的 HTML 或链接，已阻止发布。";
    }
    if (error.includes("duplicate page paths")) {
      return "模型重复生成了相同路径的页面，未发布。每次编译只能为同一页面返回一份完整内容。";
    }
    if (error.includes("invalid Wiki page path")) {
      return "模型生成的页面路径不符合 Wiki 命名或目录规则，未发布。";
    }
    const invalidFields = error.match(/invalid fields: ([a-z, ]+)\./)?.[1];
    if (invalidFields) {
      const labels: Record<string, string> = {
        summary: "处理摘要", files: "页面列表", conflicts: "冲突列表",
        sources: "来源列表", related: "关联页面", tags: "标签",
        status: "页面状态", path: "页面路径", id: "页面标识",
        created: "创建时间", updated: "更新时间"
      };
      const fields = invalidFields.split(", ").map((field) => labels[field] ?? "页面字段");
      return `模型返回的${fields.join("、")}不符合格式或长度要求，未发布。`;
    }
  }
  if (
    (error?.includes("CompilationOutputError") || error?.includes("OutputParserException")) &&
    error.includes("YAML frontmatter")
  ) {
    return "编译模型返回的页面缺少必需的 YAML 元数据或来源引用，未发布到 Wiki。已加强输出格式约束，可重试。";
  }
  if (error?.includes("CompilationOutputError") && error.includes("citations")) {
    return "编译模型返回的来源引用无效，未发布到 Wiki。请重试或检查模型输出。";
  }
  if (error?.includes("CompilationOutputError") && error.includes("metadata")) {
    return "编译模型返回的 Wiki 页标题或元数据不完整，未发布。服务现由后端确定页面 ID、类型和时间，可重试。";
  }
  if (error?.includes("CompilationOutputError")) {
    return "编译模型返回的页面未通过 Wiki 格式校验，未发布。请重试或检查模型输出。";
  }
  if (error?.includes("OpenAIInvalidRequestError") && error.includes("response_format")) {
    return "模型接口拒绝了原结构化输出格式；服务已改用兼容格式，请重试。";
  }
  if (error?.includes("Thinking mode does not support this tool_choice")) {
    return "模型的思考模式不接受强制工具选择；服务已改用 JSON 输出，请重试。";
  }
  if (error?.includes("OpenAIInvalidRequestError") &&
      (error.includes("tool_choice") || error.includes("tool calling"))) {
    return "模型接口拒绝了当前工具调用参数，请检查模型接口配置或稍后重试。";
  }
  return "资料处理失败，尚未进入 Wiki。请检查模型配置和后台任务后重试。";
}

export function ingestResult(result: SourceIngest):
  | { queued: true }
  | { queued: false; detail: string } {
  if (result.compilation_status === "queued" && result.compilation_run_id) return { queued: true };
  if (result.compilation_status === "failed") {
    return { queued: false, detail: importFailureMessage(result.compilation_error) };
  }
  return {
    queued: false,
    detail: "资料已保存，但编译状态无法确认，请查看任务状态。"
  };
}

export function batchIngestResult(result: SourceBatchIngest, expectedCount = result.items.length) {
  const accepted = result.items.filter((item) => item.revision !== null && !item.error);
  const failures = result.items.flatMap((item) => item.error
    ? [`${item.file_name}：${item.error}`]
    : item.revision ? [] : [`${item.file_name}：未返回导入版本，导入状态无法确认。`]);
  const complete = result.items.length > 0 && result.items.length === expectedCount;
  const queued = complete && accepted.length > 0 && Boolean(result.compilation_run_id) &&
    result.compilation_status === "queued" &&
    accepted.every((item) => item.revision?.compilation_run_id === result.compilation_run_id &&
      item.revision.compilation_status === "queued");
  if (accepted.length && !queued) {
    failures.push(`整批编译未成功提交：${result.compilation_status === "failed"
      ? importFailureMessage(result.compilation_error)
      : "未确认所有原件进入同一个编译任务，请刷新检查处理状态。"}`);
  }
  if (!complete) {
    failures.push("未返回全部文件的导入结果，整批导入状态无法确认，请刷新检查资料和任务状态。");
  }
  return {
    saved: accepted.length,
    queued: queued ? accepted.length : 0,
    runId: queued ? result.compilation_run_id : null,
    failures
  };
}

export function sourceRunRevisionIds(run: AgentRun): string[] {
  const ids = Array.isArray(run.input_json.source_revision_ids)
    ? run.input_json.source_revision_ids.filter((id): id is string => typeof id === "string" && id.length > 0)
    : [];
  const legacy = run.input_json.source_revision_id;
  if (typeof legacy === "string" && legacy) ids.push(legacy);
  return [...new Set(ids)];
}

export function runIncludesSourceRevision(run: AgentRun, revisionId: string | null): boolean {
  return Boolean(revisionId && sourceRunRevisionIds(run).includes(revisionId));
}

export function sourceIsIdleForCompilation(source: Source, runs: AgentRun[], drafts: Draft[]): boolean {
  if (!source.current_revision_id) return false;
  return !runs.some((run) => {
    if (!["compile_source", "parse_source"].includes(run.kind) ||
      !runIncludesSourceRevision(run, source.current_revision_id)) return false;
    if (["queued", "parsing", "running", "publishing", "completed", "published_index_failed"].includes(run.status) ||
      run.result_json?.published_commit) return true;
    if (run.status === "awaiting_review") {
      return drafts.find((draft) => draft.id === run.result_json?.draft_id)?.status !== "rejected";
    }
    return false;
  });
}

export function crawlFailureMessage(error: string | null): string {
  if (error?.includes("Queue submission failed")) {
    return "网站采集任务提交失败，请检查 Redis 和后台工作进程。";
  }
  if (error?.includes("验证")) {
    return "网站要求安全验证，请使用浏览器手动验证后继续导入，验证页不会保存为原件。";
  }
  if (error?.includes("登录")) {
    return "网站要求登录，请使用浏览器登录后导入，登录页不会保存为原件。";
  }
  if (error?.includes("有效正文")) {
    return "没有提取到有效文章正文，可尝试使用浏览器导入。";
  }
  if (error?.includes("浏览器服务")) {
    return "浏览器服务不可用，请检查服务后重试。";
  }
  return "网站采集失败，请检查网址是否可访问及网站的采集限制后重试。";
}

export function isActiveImportRun(run: AgentRun): boolean {
  return (run.kind === "compile_source" || run.kind === "crawl_site") &&
    ["queued", "parsing", "running", "publishing"].includes(run.status);
}

export function canRetrySource(status: SourceProcessingStatus, source: Source): boolean {
  return Boolean(source.current_revision_id) &&
    ["处理失败", "未编译", "未通过"].includes(status.label);
}

export function hasVisibleSourceForRun(run: AgentRun, sources: Source[]): boolean {
  return sources.some(
    (source) => runIncludesSourceRevision(run, source.current_revision_id)
  );
}

export function sourceProcessingStatus(
  source: Source,
  runs: AgentRun[],
  drafts: Draft[]
): SourceProcessingStatus {
  if (!source.current_revision_id) {
    return { label: "待处理", tone: "info", detail: "资料尚未建立可处理的版本。" };
  }
  const run = runs.find(
    (item) =>
      item.kind === "compile_source" &&
      runIncludesSourceRevision(item, source.current_revision_id)
  );
  if (!run) {
    return {
      label: "未编译",
      tone: "warning",
      detail: source.kind === "website"
        ? "网页已采集，但尚未自动编译为 Wiki 页面。"
        : "资料已保存，但未找到编译任务，请检查后台任务。"
    };
  }
  if (run.status === "failed") {
    return {
      label: "处理失败",
      tone: "danger",
      detail: importFailureMessage(run.error),
      ...(run.error?.includes("No compilation model is assigned")
        ? { action: { href: "/models" as const, label: "配置编译模型 →" } }
        : {})
    };
  }
  if (run.status === "published_index_failed") {
    return {
      label: "索引失败",
      tone: "danger",
      detail: "资料已发布，但搜索索引构建失败，请在 Wiki 中重建索引。"
    };
  }
  if (run.status === "awaiting_review") {
    const draft = drafts.find((item) => item.id === run.result_json?.draft_id);
    if (draft?.status === "published") {
      return { label: "已发布", tone: "positive", detail: "草稿已发布到 Wiki。" };
    }
    if (draft?.status === "rejected") {
      return { label: "未通过", tone: "warning", detail: "编译草稿未通过审核，资料尚未发布。" };
    }
    return {
      label: "待审核",
      tone: "warning",
      detail: "编译已完成，请审核草稿后决定是否发布到 Wiki。",
      action: { href: "/conflicts", label: "查看待审核草稿 →" }
    };
  }
  if (run.status === "completed" && typeof run.result_json?.published_commit === "string") {
    return { label: "已发布", tone: "positive", detail: "资料已编译并发布到 Wiki。" };
  }
  if (run.status === "queued") {
    return {
      label: "等待处理",
      tone: "info",
      detail: "资料已保存，正在等待解析和编译工作进程处理。"
    };
  }
  if (run.status === "parsing") {
    return { label: "解析中", tone: "info", detail: "正在解析资料，随后将编译到 Wiki。" };
  }
  if (run.status === "running" || run.status === "publishing") {
    return {
      label: run.status === "publishing" ? "发布中" : "编译中",
      tone: "info",
      detail: "后台任务正在处理资料，请稍候。"
    };
  }
  return {
    label: "待确认",
    tone: "warning",
    detail: "无法确认资料是否已发布，请检查后台任务状态。"
  };
}
