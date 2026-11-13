"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  BookOpen, ChevronRight, FileCheck2, Files, Library, MessageSquare,
  Network, ScrollText, ShieldCheck, type LucideIcon
} from "lucide-react";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState
} from "react";
import {
  ApiError,
  api,
  setActiveWikiId as persistActiveWikiId,
  type User
} from "./api";
import { ThemeToggle } from "./theme";

export type NavItem = {
  href: string;
  label: string;
};

type AppShellProps = {
  children: ReactNode;
  navItems: NavItem[];
  productName: string;
};

const PUBLIC_AUTH_PATHS = new Set(["/login", "/forgot-password"]);
const NAV_ICONS: Record<string, LucideIcon> = {
  "/wiki": Network,
  "/sources": Files,
  "/conflicts": FileCheck2,
  "/search": MessageSquare,
  "/schema": ShieldCheck,
  "/logs": ScrollText
};

export function AppShell({
  children,
  navItems,
  productName
}: AppShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [checkingAuth, setCheckingAuth] = useState(!PUBLIC_AUTH_PATHS.has(pathname));
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const workspacePage = navItems.some((item) => item.href === pathname);

  const checkAuth = useCallback(async (showLoading = true) => {
    if (showLoading) setCheckingAuth(true);
    setAuthError(null);
    try {
      const currentUser = await api.me();
      const wiki = await api.initializeWiki();
      persistActiveWikiId(wiki.id);
      setUser(currentUser);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 401) {
        router.replace(`/login?next=${encodeURIComponent(pathname)}`);
        return;
      }
      setAuthError(cause instanceof Error ? cause.message : "无法进入个人知识库。");
    } finally {
      if (showLoading) setCheckingAuth(false);
    }
  }, [pathname, router]);

  useEffect(() => {
    if (!PUBLIC_AUTH_PATHS.has(pathname) && !user && !authError) {
      void checkAuth();
    }
  }, [authError, checkAuth, pathname, user]);

  useEffect(() => {
    setSidebarCollapsed(window.localStorage.getItem("wiki-sidebar-collapsed") === "true");
  }, []);

  useEffect(() => {
    setAccountMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    function closeOnOutsideClick(event: MouseEvent) {
      if (
        accountMenuRef.current &&
        !accountMenuRef.current.contains(event.target as Node)
      ) {
        setAccountMenuOpen(false);
      }
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setAccountMenuOpen(false);
    }
    function refreshUser() {
      void checkAuth(false);
    }
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    window.addEventListener("wiki-user-updated", refreshUser);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
      window.removeEventListener("wiki-user-updated", refreshUser);
    };
  }, [checkAuth]);

  async function logout() {
    try {
      await api.logout();
    } finally {
      setUser(null);
      persistActiveWikiId(null);
      setCheckingAuth(false);
      router.replace("/login");
      router.refresh();
    }
  }

  function toggleSidebar() {
    setSidebarCollapsed((collapsed) => {
      const nextValue = !collapsed;
      window.localStorage.setItem("wiki-sidebar-collapsed", String(nextValue));
      return nextValue;
    });
  }

  if (PUBLIC_AUTH_PATHS.has(pathname)) {
    return <><div className="auth-theme-control"><ThemeToggle /></div>{children}</>;
  }

  if (checkingAuth) {
    return <div className="auth-screen" role="status"><span className="loading-dot" />正在准备个人知识库…</div>;
  }

  if (authError || !user) {
    return (
      <div className="auth-screen request-error" role="alert">
        <p>{authError ?? "当前尚未登录。"}</p>
        <button className="button" onClick={() => void checkAuth()} type="button">
          重新检查
        </button>
        <button className="button button-secondary" onClick={() => void logout()} type="button">
          退出登录
        </button>
      </div>
    );
  }

  return (
    <div
      className="app-shell"
      data-sidebar-collapsed={sidebarCollapsed}
    >
      <aside className="sidebar">
        <div className="brand">
          <span aria-hidden="true" className="brand-mark product-symbol"><BookOpen size={21} /></span>
          <strong className="brand-full">{productName}</strong>
        </div>
        <span className="nav-section-label">工作空间</span>
        <nav className="side-nav" aria-label="主导航">
          {navItems.map((item) => {
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            const Icon = NAV_ICONS[item.href] ?? BookOpen;
            return (
              <Link
                aria-current={active ? "page" : undefined}
                aria-label={item.label}
                className={active ? "nav-link nav-link-active" : "nav-link"}
                href={item.href}
                key={item.href}
                title={item.label}
              >
                <span className="nav-glyph" aria-hidden="true">
                  <Icon size={19} strokeWidth={1.8} />
                </span>
                <span className="nav-label">{item.label}</span>
              </Link>
            );
          })}
        </nav>
        <div className="sidebar-workspace" title="个人知识库">
          <Library aria-hidden="true" size={20} />
          <div><strong>个人知识库</strong><span>让知识持续生长</span></div>
        </div>
        <button
          aria-label={sidebarCollapsed ? "展开左侧菜单" : "收起左侧菜单"}
          className="sidebar-collapse-button"
          onClick={toggleSidebar}
          title={sidebarCollapsed ? "展开左侧菜单" : "收起左侧菜单"}
          type="button"
        >
          <svg aria-hidden="true" fill="none" height="16" viewBox="0 0 16 16" width="16">
            <path d="M10 3.5 5.5 8l4.5 4.5" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" />
          </svg>
        </button>
      </aside>
      <div className={workspacePage ? "shell-content shell-workspace" : "shell-content"}>
        <header className="topbar">
          <div className="topbar-context">
            <Library aria-hidden="true" size={16} />
            <span>个人知识空间</span>
            <ChevronRight aria-hidden="true" size={14} />
            <strong>{navItems.find((item) => pathname.startsWith(item.href))?.label
              ?? (pathname === "/models" ? "模型配置" : pathname === "/profile" ? "个人信息" : "个人知识库")}</strong>
          </div>
          <div className="topbar-actions">
            <ThemeToggle />
            <span className="api-state">{user.display_name}</span>
            <div className="account-menu" ref={accountMenuRef}>
              <button
                aria-expanded={accountMenuOpen}
                aria-haspopup="menu"
                aria-label="打开账号菜单"
                className="avatar"
                onClick={() => setAccountMenuOpen((open) => !open)}
                title="账号菜单"
                type="button"
              >
                {user.avatar_url ? (
                  <img alt="" src={api.avatarUrl(user.updated_at)} />
                ) : (
                  initials(user.display_name)
                )}
              </button>
              {accountMenuOpen ? (
                <div aria-label="账号菜单" className="account-dropdown" role="menu">
                  <div className="account-dropdown-user">
                    <strong>{user.display_name}</strong>
                    <span>{user.email ?? "未绑定邮箱"}</span>
                  </div>
                  <Link
                    className="account-dropdown-item"
                    href="/profile"
                    onClick={() => setAccountMenuOpen(false)}
                    role="menuitem"
                  >
                    个人信息
                  </Link>
                  <Link
                    className="account-dropdown-item"
                    href="/models"
                    onClick={() => setAccountMenuOpen(false)}
                    role="menuitem"
                  >
                    模型配置
                  </Link>
                  <button
                    className="account-dropdown-item account-dropdown-logout"
                    onClick={() => void logout()}
                    role="menuitem"
                    type="button"
                  >
                    退出登录
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </header>
        {user.must_change_password || !user.email ? (
          <div className="form-notice security-recommendation" role="note">
            <span>
              安全建议：{[
                ...(user.must_change_password ? ["修改初始密码"] : []),
                ...(!user.email ? ["绑定邮箱，方便找回密码"] : [])
              ].join("，")}。此步骤可选，不影响使用。
            </span>
            <Link href="/profile">前往个人信息</Link>
          </div>
        ) : null}
        <main
          className={workspacePage ? "main-content workspace-content" : "main-content"}
          data-page={pathname}
        >
          {children}
        </main>
      </div>
    </div>
  );
}

function initials(displayName: string): string {
  return displayName
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => part[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}
