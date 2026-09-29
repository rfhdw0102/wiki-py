import type { Metadata } from "next";
import { AppShell, type NavItem } from "@wiki-agent/web-ui";
import "@wiki-agent/web-ui/styles.css";

export const metadata: Metadata = {
  title: {
    default: "LLM Wiki",
    template: "%s | LLM Wiki"
  },
  description: "Build and explore an AI-assisted knowledge workspace."
};

const navItems: NavItem[] = [
  { href: "/", label: "Overview", shortLabel: "OV" },
  { href: "/sources", label: "Sources", shortLabel: "SO" },
  { href: "/wiki", label: "Wiki", shortLabel: "WI" },
  { href: "/drafts", label: "Drafts", shortLabel: "DR" },
  { href: "/tasks", label: "Agent tasks", shortLabel: "AT" },
  { href: "/conflicts", label: "Conflicts", shortLabel: "CF" },
  { href: "/search", label: "Search", shortLabel: "SE" }
];

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AppShell
          navItems={navItems}
          productName="LLM Wiki"
          productSubtitle="Knowledge workspace"
          theme="user"
        >
          {children}
        </AppShell>
      </body>
    </html>
  );
}
