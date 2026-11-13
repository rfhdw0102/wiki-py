import type { AgentRun } from "@wiki-agent/web-ui";

export type WebsiteScope = "page" | "site";
export type CrawlIssue = { url: string; message: string; code: string };

export function websiteImportPayload(value: string, scope: WebsiteScope) {
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    throw new Error("请输入完整的 HTTP 或 HTTPS 网址。");
  }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("仅支持不包含账号密码的 HTTP 或 HTTPS 网址。");
  }
  if (url.href.length > 8192) throw new Error("网址过长，请检查后重试。");
  url.hash = "";
  return { url: url.href, max_depth: scope === "site" ? 1 : 0, max_pages: scope === "site" ? 20 : 1 };
}

const ISSUE_MESSAGES: Record<string, string> = {
  verification_required: "网站要求安全验证，请使用浏览器手动验证后导入。",
  login_required: "网站要求登录，请使用浏览器登录后导入。",
  empty_content: "未提取到有效正文，可尝试浏览器渲染后导入。",
  browser_required: "浏览器自动读取未取得正文，需要手动验证或登录。",
  browser_unavailable: "浏览器服务不可用，请检查服务后重试。",
  robots_blocked: "网站禁止自动采集该页面。",
  unsafe_url: "该网址不是允许访问的公网地址。",
  content_unavailable: "文章不存在、已删除或不可访问。",
  too_large: "网页超过允许的导入大小，未导入。",
  unsupported_content: "该网址没有返回可导入的 HTML 或文本正文。",
  fetch_failed: "网页请求失败，请检查网址是否可访问。"
};
const BROWSER_ISSUES = new Set([
  "verification_required", "login_required", "empty_content", "browser_required", "browser_unavailable"
]);

export function crawlImportSummary(run: AgentRun) {
  const result = run.result_json;
  const revisions = result?.source_revision_ids;
  const imported = typeof result?.imported_count === "number"
    ? result.imported_count : Array.isArray(revisions) ? revisions.length : 0;
  const rawErrors = result?.errors;
  const errors: CrawlIssue[] = Array.isArray(rawErrors) ? rawErrors.flatMap((item: unknown) => {
    if (!item || typeof item !== "object" || !("url" in item) || typeof item.url !== "string") return [];
    const code = "code" in item && typeof item.code === "string" ? item.code : "fetch_failed";
    return [{ url: item.url, code, message: ISSUE_MESSAGES[code] ?? "页面未能导入，请使用浏览器检查。" }];
  }) : [];
  const failedCount = typeof result?.failed_count === "number" ? result.failed_count : errors.length;
  const candidate = errors.find((item) => BROWSER_ISSUES.has(item.code))?.url ??
    (result?.browser_required === true && typeof run.input_json.url === "string" ? run.input_json.url : null);
  let browserUrl: string | null = null;
  if (candidate) {
    try {
      browserUrl = websiteImportPayload(candidate, "page").url;
    } catch {
      browserUrl = null;
    }
  }
  const failed = imported === 0 || failedCount > 0 || run.status === "failed";
  const message = imported === 0
    ? "没有导入有效正文，验证页和登录页不会保存为原件。"
    : `已导入 ${imported} 篇正文${failedCount ? `，${failedCount} 个页面未导入` : ""}。原件已保存，可手动编译为 Wiki。`;
  return { message, failed, errors, browserUrl };
}
