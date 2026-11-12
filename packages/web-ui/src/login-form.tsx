"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { api } from "./api";
import { SliderCaptcha } from "./slider-captcha";
import { AuthShowcase } from "./auth-showcase";

type PendingCredentials = {
  username: string;
  password: string;
};

export function LoginForm({ productName }: { productName: string }) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [captchaOpen, setCaptchaOpen] = useState(false);
  const [pendingCredentials, setPendingCredentials] =
    useState<PendingCredentials | null>(null);
  const [captchaKey, setCaptchaKey] = useState(0);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const form = new FormData(event.currentTarget);
    setPendingCredentials({
      username: String(form.get("username")),
      password: String(form.get("password"))
    });
    setCaptchaKey((current) => current + 1);
    setCaptchaOpen(true);
  }

  async function finishLogin(captchaToken: string | null) {
    if (!captchaToken || !pendingCredentials) return;
    setSubmitting(true);
    try {
      await api.login(
        pendingCredentials.username,
        pendingCredentials.password,
        captchaToken
      );
      const requested = new URLSearchParams(window.location.search).get("next");
      const destination =
        requested?.startsWith("/") && !requested.startsWith("//") ? requested : "/";
      router.replace(destination);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败，请稍后重试。");
    } finally {
      setSubmitting(false);
      setCaptchaOpen(false);
      setPendingCredentials(null);
    }
  }

  function closeCaptcha() {
    if (submitting) return;
    setCaptchaOpen(false);
    setPendingCredentials(null);
  }

  return (
    <main className="login-screen" data-login-theme="user">
      <AuthShowcase productName={productName} title={"把资料变成你的知识。"} />
      <section className="login-entry">
        <form className="login-card" onSubmit={submit}>
          <div className="login-form-head">
            <h2>欢迎回来</h2>
          </div>
          <label>
            账号
            <input autoComplete="username" name="username" placeholder="admin" required />
          </label>
          <label>
            密码
            <input
              autoComplete="current-password"
              minLength={8}
              name="password"
              placeholder="请输入登录密码"
              required
              type="password"
            />
          </label>
          {error ? <div className="form-error" role="alert">{error}</div> : null}
          <button
            className="button login-submit"
            disabled={submitting}
            type="submit"
          >
            {submitting ? "正在登录…" : "进入工作空间"}
          </button>
          <div className="auth-links">
            <Link href="/forgot-password">忘记密码</Link>
          </div>
          <p className="login-hint">
            登录后即使修改密码并绑定邮箱。
          </p>
        </form>
      </section>
      {captchaOpen ? (
        <div
          aria-labelledby="captcha-dialog-title"
          aria-modal="true"
          className="captcha-dialog-backdrop"
          role="dialog"
        >
          <div className="captcha-dialog">
            <div className="captcha-dialog-head">
              <div>
                <span className="eyebrow">登录安全验证</span>
                <h2 id="captcha-dialog-title">请完成滑块验证</h2>
                <p>验证通过后将自动登录。</p>
              </div>
              <button
                aria-label="关闭验证"
                className="captcha-dialog-close"
                disabled={submitting}
                onClick={closeCaptcha}
                type="button"
              >
                ×
              </button>
            </div>
            <SliderCaptcha
              key={captchaKey}
              onVerified={(token) => void finishLogin(token)}
            />
            {submitting ? (
              <div className="captcha-login-state" role="status">
                <span className="loading-dot" />
                正在登录…
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </main>
  );
}
