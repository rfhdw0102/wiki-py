"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { api, type UserRole } from "./api";

export function LoginForm({
  productName,
  requiredRole
}: {
  productName: string;
  requiredRole?: UserRole;
}) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    const form = new FormData(event.currentTarget);
    try {
      const user = await api.login(String(form.get("email")), String(form.get("password")));
      if (requiredRole && user.role !== requiredRole) {
        await api.logout();
        throw new Error("This account does not have administrator access.");
      }
      const requested = new URLSearchParams(window.location.search).get("next");
      const destination =
        requested?.startsWith("/") && !requested.startsWith("//") ? requested : "/";
      router.replace(destination);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-screen">
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        <div className="brand-mark" aria-hidden="true">W</div>
        <span className="eyebrow">{productName}</span>
        <h1>Sign in</h1>
        <p>Use your Wiki account to continue.</p>
        <label>
          Email
          <input autoComplete="email" name="email" required type="email" />
        </label>
        <label>
          Password
          <input
            autoComplete="current-password"
            minLength={8}
            name="password"
            required
            type="password"
          />
        </label>
        {error ? <div className="form-error" role="alert">{error}</div> : null}
        <button className="button" disabled={submitting} type="submit">
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
