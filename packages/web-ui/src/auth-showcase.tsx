import { ArrowRight, BookOpen, Files, MessageSquare, Network } from "lucide-react";

export function AuthShowcase({ productName, title }: { productName: string; title: string }) {
  return (
    <section className="login-showcase">
      <div className="login-brand">
        <span className="product-symbol"><BookOpen aria-hidden="true" size={22} /></span>
        <strong>{productName}</strong>
      </div>
      <div className="login-story">
        <h1>{title}</h1>
        <div className="login-preview" aria-hidden="true">
          <div className="login-preview-head">
            <span><Network size={17} />我的知识空间</span>
            <span className="login-preview-label">资料 → 知识 → 灵感</span>
          </div>
          <div className="login-preview-flow">
            <div><Files size={22} /><strong>收集资料</strong><span>文档与网页</span></div>
            <ArrowRight size={18} />
            <div><BookOpen size={22} /><strong>连接知识</strong><span>可追溯的 Wiki</span></div>
            <ArrowRight size={18} />
            <div><MessageSquare size={22} /><strong>探索答案</strong><span>基于证据的问答</span></div>
          </div>
        </div>
      </div>
      <p className="login-showcase-foot">个人知识空间 · 为你的思考而构建</p>
    </section>
  );
}
