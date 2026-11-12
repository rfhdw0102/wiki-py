"use client";

import Link from "next/link";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { api } from "./api";
import { AuthShowcase } from "./auth-showcase";

type AuthFrameProps = {
  children: ReactNode;
  productName: string;
  title: string;
};

function AuthFrame({
  children,
  productName,
  title
}: AuthFrameProps) {
  return (
    <main className="login-screen" data-login-theme="user">
      <AuthShowcase productName={productName} title={title} />
      <section className="login-entry">{children}</section>
    </main>
  );
}

function useCountdown() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (seconds <= 0) return;
    const timer = window.setTimeout(
      () => setSeconds((current) => Math.max(0, current - 1)),
      1000
    );
    return () => window.clearTimeout(timer);
  }, [seconds]);
  return [seconds, setSeconds] as const;
}

export function PasswordRecoveryForm({
  productName
}: {
  productName: string;
}) {
  const [email, setEmail] = useState("");
  const [countdown, setCountdown] = useCountdown();
  const [sending, setSending] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);

  async function sendCode() {
    setError(null);
    setNotice(null);
    if (!email) {
      setError("请先填写账号邮箱。");
      return;
    }
    setSending(true);
    try {
      await api.sendPasswordResetCode(email);
      setNotice("如果该账号存在，验证码会发送到对应邮箱。");
      setCountdown(60);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "验证码发送失败。");
    } finally {
      setSending(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const form = new FormData(event.currentTarget);
    const password = String(form.get("password"));
    if (password !== String(form.get("password_confirmation"))) {
      setError("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    try {
      await api.recoverPassword({
        email,
        password,
        email_code: String(form.get("email_code"))
      });
      setComplete(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "密码重置失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <AuthFrame
      productName={productName}
      title="重新获得访问权限。"
    >
      {complete ? (
        <div className="login-card password-complete">
          <span className="completion-mark" aria-hidden="true">✓</span>
          <div className="login-form-head">
            <span className="eyebrow">密码已更新</span>
            <h2>可以重新登录了</h2>
            <p>所有旧会话均已撤销，请使用新密码进入系统。</p>
          </div>
          <Link className="button login-submit button-link" href="/login">返回登录</Link>
        </div>
      ) : (
        <form className="login-card auth-card-wide" onSubmit={(event) => void submit(event)}>
          <div className="login-form-head">
            <h2>重置密码</h2>
          </div>
          <label>
            账号邮箱
            <input
              autoComplete="email"
              name="email"
              onChange={(event) => setEmail(event.target.value)}
              placeholder="请输入账号邮箱"
              required
              type="email"
              value={email}
            />
          </label>
          <p className="login-hint">尚未绑定邮箱的账号无法通过邮件找回密码。</p>
          <div className="verification-row">
            <label>
              邮箱验证码
              <input
                autoComplete="one-time-code"
                inputMode="numeric"
                maxLength={6}
                name="email_code"
                pattern="\d{6}"
                placeholder="6 位数字"
                required
              />
            </label>
            <button
              className="button button-secondary code-button"
              disabled={sending || countdown > 0 || !email}
              onClick={() => void sendCode()}
              type="button"
            >
              {sending ? "发送中…" : countdown > 0 ? `${countdown} 秒后重发` : "发送验证码"}
            </button>
          </div>
          <label>
            新密码
            <input
              autoComplete="new-password"
              minLength={12}
              name="password"
              placeholder="至少 12 个字符"
              required
              type="password"
            />
          </label>
          <label>
            确认新密码
            <input
              autoComplete="new-password"
              minLength={12}
              name="password_confirmation"
              placeholder="再次输入新密码"
              required
              type="password"
            />
          </label>
          {notice ? <div className="form-notice" role="status">{notice}</div> : null}
          {error ? <div className="form-error" role="alert">{error}</div> : null}
          <button className="button login-submit" disabled={submitting} type="submit">
            {submitting ? "正在更新…" : "重置密码"}
          </button>
          <div className="auth-links auth-links-centered">
            <Link href="/login">返回登录</Link>
          </div>
        </form>
      )}
    </AuthFrame>
  );
}
