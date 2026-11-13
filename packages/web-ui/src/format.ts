const labels: Record<string, string> = {
  active: "启用",
  answer: "问答",
  awaiting_review: "等待审核",
  chat: "对话模型",
  compilation: "知识编译",
  completed: "已完成",
  crawl: "网站采集",
  disabled: "已停用",
  embedding: "向量模型",
  failed: "失败",
  html: "网页",
  manual_review: "人工审核",
  markdown: "Markdown",
  open: "待发布",
  parse: "文档解析",
  pending: "等待中",
  planning: "任务规划",
  published: "已发布",
  queued: "已排队",
  raw: "原始资料",
  reranker: "重排模型",
  reranking: "结果重排",
  running: "执行中",
  text: "文本",
  user: "普通用户",
  wiki: "知识库"
};

export function formatDateTime(value: string | number | Date) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false
  }).format(new Date(value));
}

export function formatLabel(value: unknown) {
  const text = String(value);
  return labels[text] ?? text.replaceAll("_", " ");
}
