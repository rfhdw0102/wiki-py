"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { ApiError, api, type User } from "./api";

export type NavItem = {
  href: string;
  label: string;
  shortLabel: string;
};

type AppShellProps = {
  children: ReactNode;
  navItems: NavItem[];
  productName: string;
  productSubtitle: string;
  theme: "user" | "admin";
};

export function AppShell({
  children,
  navItems,
  productName,
  productSubtitle,
  theme
}: AppShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [checkingAuth, setCheckingAuth] = useState(pathname !== "/login");

  const checkAuth = useCallback(async () => {
    setCheckingAuth(true);
    setAuthError(null);
    try {
      const currentUser = await api.me();
      if (theme === "admin" && currentUser.role !== "admin") {
        setAuthError("Administrator access is required.");
        return;
      }
      setUser(currentUser);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
        return;
      }
      setAuthError(cause instanceof Error ? cause.message : "Unable to verify your session.");
    } finally {
      setCheckingAuth(false);
    }
  }, [pathname, router, theme]);

  useEffect(() => {
    if (pathname !== "/login") void checkAuth();
  }, [checkAuth, pathname]);

  async function logout() {
    try {
      await api.logout();
    } finally {
      router.replace("/login");
      router.refresh();
    }
  }

  if (pathname === "/login") {
    return <>{children}</>;
  }

  if (checkingAuth) {
    return <div className="auth-screen" role="status">Checking your session…</div>;
  }

  if (authError || !user) {
    return (
      <div className="auth-screen request-error" role="alert">
        <p>{authError ?? "You are not signed in."}</p>
        <button className="button" onClick={() => void checkAuth()} type="button">
          Retry
        </button>
        <button className="button button-secondary" onClick={() => void logout()} type="button">
          Sign out
        </button>
      </div>
    );
  }

  return (
    <div className="app-shell" data-theme={theme}>
      <aside className={menuOpen ? "sidebar sidebar-open" : "sidebar"}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            W
          </span>
          <div>
            <strong>{productName}</strong>
            <span>{productSubtitle}</span>
          </div>
        </div>
        <nav className="side-nav" aria-label="Primary navigation">
          {navItems.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                aria-current={active ? "page" : undefined}
                className={active ? "nav-link nav-link-active" : "nav-link"}
                href={item.href}
                key={item.href}
                onClick={() => setMenuOpen(false)}
              >
                <span className="nav-glyph" aria-hidden="true">
                  {item.shortLabel}
                </span>
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <span className="status-dot" />
          Connected to API
        </div>
      </aside>
      <div className="shell-content">
        <header className="topbar">
          <button
            aria-expanded={menuOpen}
            aria-label="Toggle navigation"
            className="menu-button"
            onClick={() => setMenuOpen((open) => !open)}
            type="button"
          >
            <span />
            <span />
            <span />
          </button>
          <div className="topbar-context">
            <span>Workspace</span>
            <strong>LLM Wiki</strong>
          </div>
          <div className="topbar-actions">
            <span className="api-state">{user.email}</span>
            <button
              className="avatar"
              aria-label="Sign out"
              onClick={() => void logout()}
              title="Sign out"
              type="button"
            >
              {user.display_name
                .split(/\s+/)
                .map((part) => part[0])
                .join("")
                .slice(0, 2)
                .toUpperCase()}
            </button>
          </div>
        </header>
        <main className="main-content">{children}</main>
      </div>
      {menuOpen ? (
        <button
          aria-label="Close navigation"
          className="sidebar-backdrop"
          onClick={() => setMenuOpen(false)}
          type="button"
        />
      ) : null}
    </div>
  );
}
