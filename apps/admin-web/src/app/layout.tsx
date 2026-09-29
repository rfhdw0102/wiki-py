import type { Metadata } from "next";
import { AppShell, type NavItem } from "@wiki-agent/web-ui";
import "@wiki-agent/web-ui/styles.css";

export const metadata: Metadata = {
  title: {
    default: "LLM Wiki Control",
    template: "%s | Wiki Control"
  },
  description: "Administration console for the LLM Wiki platform."
};

const navItems: NavItem[] = [
  { href: "/", label: "System overview", shortLabel: "OV" },
  { href: "/users", label: "Users", shortLabel: "US" },
  { href: "/sources", label: "Sources", shortLabel: "SO" },
  { href: "/models", label: "Models", shortLabel: "MO" },
  { href: "/feature-flags", label: "Feature flags", shortLabel: "FF" },
  { href: "/schema", label: "Schema", shortLabel: "SC" },
  { href: "/jobs", label: "Jobs", shortLabel: "JB" },
  { href: "/knowledge-gaps", label: "Knowledge gaps", shortLabel: "KG" },
  { href: "/audit", label: "Audit", shortLabel: "AU" }
];

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <AppShell
          navItems={navItems}
          productName="Wiki Control"
          productSubtitle="Administration"
          theme="admin"
        >
          {children}
        </AppShell>
      </body>
    </html>
  );
}
