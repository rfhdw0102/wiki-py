import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { StatusBadge, type WikiQuestionResponse } from "@wiki-agent/web-ui";

type Evidence = WikiQuestionResponse["evidence"][number];
type MarkdownNode = {
  type: string;
  value?: string;
  children?: MarkdownNode[];
};

function evidenceHref({ document_id: id, metadata }: Evidence): string | undefined {
  if (id.startsWith("raw:")) {
    const revision = metadata.source_revision_id;
    const span = metadata.span_id;
    return typeof revision === "string" && revision && typeof span === "string" && span
      && id === `raw:${span}`
      ? `/sources?revision=${encodeURIComponent(revision)}&span=${encodeURIComponent(span)}`
      : undefined;
  }
  if (/^(?:sources|entities|concepts|pending)\/[^/\\]+\.md$/.test(id)) {
    return `/wiki?path=${encodeURIComponent(id)}`;
  }
}

function citeEvidence(links: ReadonlyMap<string, { number: number; href: string }>) {
  return () => (tree: MarkdownNode) => {
    function visit(node: MarkdownNode) {
      if (!node.children || ["link", "linkReference", "footnoteDefinition"].includes(node.type)) {
        return;
      }
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || !child.value) {
          visit(child);
          return [child];
        }
        const parts = child.value.split(/(\[\^[^\]\r\n]+\])/g);
        return parts.filter(Boolean).map((part) => {
          const citation = part.startsWith("[^") && part.endsWith("]")
            ? links.get(part.slice(2, -1)) : undefined;
          return citation ? {
            type: "link",
            url: citation.href,
            data: { hProperties: { className: ["answer-citation"] } },
            children: [{ type: "text", value: `[${citation.number}]` }]
          } : { type: "text", value: part };
        });
      });
    }
    visit(tree);
  };
}

export function AnswerMessage({
  answer,
  copied,
  onCopy
}: {
  answer: WikiQuestionResponse;
  copied: boolean;
  onCopy: () => void;
}) {
  const evidenceById = new Map(answer.evidence.map((item) => [item.document_id, item]));
  const references = [...new Set(answer.citations)].flatMap((id, index) => {
    const item = evidenceById.get(id);
    return item ? [{ item, number: index + 1, href: evidenceHref(item) }] : [];
  });
  const links = new Map(references.flatMap(({ item, number, href }) =>
    href ? [[item.document_id, { number, href }] as const] : []
  ));
  const allowedLinks = new Set([...links.values()].map(({ href }) => href));
  const clarification = Boolean(answer.clarification_question);
  const noEvidence = answer.insufficient_evidence && !clarification;
  const legacyFallback = noEvidence
    && answer.answer === "No supporting evidence was found in the Wiki or enabled raw sources.";
  const body = legacyFallback ? "知识库中没有找到能支持这个问题的资料。" : answer.answer;
  const uncertainty = noEvidence && (legacyFallback || !answer.uncertainty)
    ? "可以补充具体名称、型号或时间范围，或导入相关资料后再提问。"
    : answer.uncertainty;

  return (
    <>
      <div className="assistant-answer wiki-rendered">
        <ReactMarkdown
          remarkPlugins={[remarkGfm, citeEvidence(links)]}
          skipHtml
          disallowedElements={["img"]}
          components={{
            a: ({ href, className, children }) => href && allowedLinks.has(href)
              ? <a href={href} className={className}>{children}</a>
              : <>{children}</>,
            table: ({ children }) => <div className="answer-table"><table>{children}</table></div>
          }}
        >{body}</ReactMarkdown>
      </div>
      {uncertainty && !clarification ? (
        <div className="answer-uncertainty">{uncertainty}</div>
      ) : null}
      {answer.degradations.map((degradation) => (
        <div className="answer-uncertainty" key={degradation.channel}>
          {degradation.reason}
        </div>
      ))}
      {references.length ? (
        <div className="answer-evidence">
          <strong>参考来源</strong>
          <div className="evidence-list">
            {references.map(({ item, number, href }) => {
              const { title, heading, source_name } = item.metadata;
              const label = [title, source_name, heading]
                .find((value): value is string => typeof value === "string" && value.trim() !== "")
                ?? item.document_id;
              const Card = href ? "a" : "article";
              return (
                <Card className="evidence-card" key={item.document_id} href={href}>
                  <span>{number}</span>
                  <div>
                    <strong>{label}</strong>
                    <small>{heading && typeof heading === "string" && heading !== label ? heading
                      : item.channels.join(" · ") || "知识库"}</small>
                  </div>
                </Card>
              );
            })}
          </div>
        </div>
      ) : null}
      <div className="assistant-message-actions">
        {clarification ? <StatusBadge tone="neutral">需要澄清</StatusBadge> : (
          <>
            <StatusBadge tone={noEvidence ? "warning" : "positive"}>
              {noEvidence ? "证据不足" : "已有证据支持"}
            </StatusBadge>
            <span>
              {answer.layer === "hybrid" ? "Wiki + Raw 融合" : answer.layer === "raw" ? "Raw 兜底" : "Wiki"}
            </span>
            {answer.reranker_used ? <span>已重排</span> : null}
            <span>{references.length} 条引用</span>
          </>
        )}
        <button onClick={onCopy} type="button">{copied ? "已复制" : "复制回答"}</button>
      </div>
    </>
  );
}
