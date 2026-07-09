"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";
import { List, Plus, Settings, User, Building, Tag } from "@/components/ui/icons";
import { BRAND_NAME } from "@/lib/brand";

interface NavItem {
  href: string;
  label: string;
  icon: React.ReactNode;
}

const mainNav: NavItem[] = [
  { href: "/runs", label: "Sourcing runs", icon: <List size={14} /> },
  { href: "/runs/new", label: "New run", icon: <Plus size={14} /> },
];

const adminNav: NavItem[] = [
  { href: "/facilities", label: "Facilities", icon: <Building size={14} /> },
  { href: "/rules", label: "Approval rules", icon: <Tag size={14} /> },
  { href: "/settings", label: "Settings", icon: <Settings size={14} /> },
];

interface SidebarNavProps {
  className?: string;
}

export function SidebarNav({ className }: SidebarNavProps) {
  const pathname = usePathname();

  return (
    <nav className={cn("flex h-full flex-col", className)}>
      {/* Wordmark */}
      <div className="flex h-12 items-center gap-2.5 border-b border-hr-2 px-4">
        <div
          className="flex h-6 w-6 items-center justify-center rounded"
          style={{
            background: "linear-gradient(135deg, var(--blue-50), #2563eb)",
            fontFamily: "var(--font-jetbrains)",
            fontWeight: 700,
            fontSize: 13,
            color: "#07101e",
          }}
        >
          A
        </div>
        <span
          className="font-mono text-[11px] font-semibold uppercase tracking-[0.10em] text-fg-2"
        >
          {BRAND_NAME}
        </span>
      </div>

      {/* Main nav */}
      <div className="flex flex-col gap-0.5 px-2 pt-3">
        <NavSection label="Workspace">
          {mainNav.map((item) => (
            <NavLink key={item.href} item={item} active={pathname === item.href || pathname.startsWith(item.href + "/")} />
          ))}
        </NavSection>
      </div>

      {/* Admin nav — pushed to bottom */}
      <div className="mt-auto flex flex-col gap-0.5 border-t border-hr-2 px-2 py-3">
        <NavSection label="Admin">
          {adminNav.map((item) => (
            <NavLink key={item.href} item={item} active={pathname === item.href} />
          ))}
        </NavSection>

        {/* User */}
        <div className="mt-1 flex items-center gap-2 rounded px-2.5 py-1.5 text-fg-3">
          <User size={14} className="shrink-0" />
          <span className="font-mono text-[11px] truncate">Procurement</span>
        </div>
      </div>
    </nav>
  );
}

function NavSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="mb-1 px-2.5 font-mono text-[10px] uppercase tracking-[0.10em] text-fg-4">
        {label}
      </p>
      {children}
    </div>
  );
}

function NavLink({ item, active }: { item: NavItem; active: boolean }) {
  return (
    <Link
      href={item.href}
      className={cn(
        "flex items-center gap-2.5 rounded px-2.5 py-1.5 transition-colors",
        "font-mono text-[11.5px] tracking-[-0.005em]",
        active
          ? "bg-blue-tint text-blue-fg border border-blue-line"
          : "text-fg-3 hover:bg-bg-3 hover:text-fg-1 border border-transparent",
      )}
    >
      <span className="shrink-0">{item.icon}</span>
      <span className="truncate">{item.label}</span>
    </Link>
  );
}
