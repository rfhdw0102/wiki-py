"use client";

import {
  DataTable,
  EmptyState,
  PageHeader,
  RequestState,
  Section,
  StatusBadge,
  api,
  type User,
  type UserRole,
  useApiResource
} from "@wiki-agent/web-ui";
import { type FormEvent, useCallback, useState } from "react";

export default function UsersPage() {
  const load = useCallback(() => api.listUsers(), []);
  const { data, error, loading, reload } = useApiResource(load);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [updating, setUpdating] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [passwords, setPasswords] = useState<Record<string, string>>({});

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setCreating(true);
    setCreateError(null);
    try {
      await api.createUser({
        display_name: String(form.get("display_name")),
        email: String(form.get("email")),
        password: String(form.get("password")),
        role: String(form.get("role")) as UserRole
      });
      formElement.reset();
      reload();
    } catch (cause) {
      setCreateError(cause instanceof Error ? cause.message : "Unable to create user.");
    } finally {
      setCreating(false);
    }
  }

  async function toggleStatus(user: User) {
    setUpdating(user.id);
    setActionError(null);
    try {
      await api.updateUserStatus(user.id, !user.is_active);
      reload();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Unable to update the user.");
    } finally {
      setUpdating(null);
    }
  }

  async function resetPassword(user: User) {
    const password = passwords[user.id] ?? "";
    if (password.length < 12) {
      setActionError("Replacement passwords must contain at least 12 characters.");
      return;
    }
    setUpdating(user.id);
    setActionError(null);
    try {
      await api.resetUserPassword(user.id, password);
      setPasswords((current) => ({ ...current, [user.id]: "" }));
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "Unable to reset the password.");
    } finally {
      setUpdating(null);
    }
  }

  return (
    <>
      <PageHeader
        description="Create identities and review current access and account status."
        eyebrow="Identity and access"
        title="Users"
      />
      <Section subtitle="Passwords must contain at least 12 characters" title="Create user">
        <form className="form-grid" onSubmit={(event) => void create(event)}>
          <label className="field">Display name<input name="display_name" required /></label>
          <label className="field">Email<input name="email" required type="email" /></label>
          <label className="field">Password<input minLength={12} name="password" required type="password" /></label>
          <label className="field">
            Role
            <select defaultValue="user" name="role">
              <option value="user">User</option>
              <option value="admin">Administrator</option>
            </select>
          </label>
          {createError ? <div className="form-error" role="alert">{createError}</div> : null}
          <div className="form-actions">
            <button className="button" disabled={creating} type="submit">
              {creating ? "Creating…" : "Create user"}
            </button>
          </div>
        </form>
      </Section>
      <Section subtitle="Accounts returned by the administration API" title="User directory">
        {actionError ? <div className="request-state request-error" role="alert">{actionError}</div> : null}
        <RequestState error={error} loading={loading} onRetry={reload} />
        {!loading && !error && data?.length === 0 ? (
          <EmptyState title="No users" description="Create the first user above." />
        ) : null}
        {data?.length ? (
          <DataTable<User>
            columns={[
              { key: "display_name", label: "Name" },
              { key: "email", label: "Email" },
              { key: "role", label: "Role", render: (value) => <StatusBadge tone={value === "admin" ? "info" : "neutral"}>{String(value)}</StatusBadge> },
              { key: "is_active", label: "Status", render: (value) => <StatusBadge tone={value ? "positive" : "danger"}>{value ? "Active" : "Disabled"}</StatusBadge> },
              {
                key: "id",
                label: "Account actions",
                render: (_, row) => (
                  <div className="inline-form">
                    <button
                      className="button button-secondary button-small"
                      disabled={updating === row.id}
                      onClick={() => void toggleStatus(row)}
                      type="button"
                    >
                      {row.is_active ? "Disable" : "Enable"}
                    </button>
                    <input
                      aria-label={`Replacement password for ${row.display_name}`}
                      minLength={12}
                      onChange={(event) =>
                        setPasswords((current) => ({
                          ...current,
                          [row.id]: event.target.value
                        }))
                      }
                      placeholder="New password"
                      type="password"
                      value={passwords[row.id] ?? ""}
                    />
                    <button
                      className="button button-secondary button-small"
                      disabled={updating === row.id}
                      onClick={() => void resetPassword(row)}
                      type="button"
                    >
                      Reset password
                    </button>
                  </div>
                )
              }
            ]}
            rowKey="id"
            rows={data}
          />
        ) : null}
      </Section>
    </>
  );
}
