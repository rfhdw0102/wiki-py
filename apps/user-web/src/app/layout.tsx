import type { Metadata } from "next";
import { AppShell, ThemeProvider, THEME_INIT_SCRIPT, type NavItem } from "@wiki-agent/web-ui";
import "@wiki-agent/web-ui/styles.css";
import "@xyflow/react/dist/style.css";

export const metadata: Metadata = {
  title: {
    default: "智库 Wiki",
    template: "%s | 智库 Wiki"
  },
  description: "将个人资料编译成可追溯、可演进的知识 Wiki。"
};

const navItems: NavItem[] = [
  { href: "/wiki", label: "知识 Wiki" },
  { href: "/sources", label: "文件导入" },
  { href: "/conflicts", label: "审核冲突" },
  { href: "/search", label: "知识问答" },
  { href: "/schema", label: "知识规范" },
  { href: "/logs", label: "运行日志" }
];

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
      </head>
      <body>
        <ThemeProvider>
          <AppShell
            navItems={navItems}
            productName="智库 Wiki"
          >
            {children}
          </AppShell>
        </ThemeProvider>
      </body>
    </html>
  );
}
