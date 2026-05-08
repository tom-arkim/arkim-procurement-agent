import { cn } from "@/lib/utils";

interface AppLayoutProps {
  sidebar: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function AppLayout({ sidebar, children, className }: AppLayoutProps) {
  return (
    <div
      className={cn(
        "flex h-screen w-full overflow-hidden",
        "bg-bg-1 text-fg-1",
        className,
      )}
    >
      {/* Sidebar */}
      <aside className="flex h-full w-[220px] shrink-0 flex-col border-r border-hr-2 bg-bg-0">
        {sidebar}
      </aside>

      {/* Main content */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {children}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Scrollable content region with optional top bar
// ---------------------------------------------------------------------------

interface ContentAreaProps {
  topBar?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function ContentArea({ topBar, children, className }: ContentAreaProps) {
  return (
    <div className={cn("flex flex-1 flex-col overflow-hidden", className)}>
      {topBar && (
        <div className="shrink-0 border-b border-hr-2">
          {topBar}
        </div>
      )}
      <div className="flex-1 overflow-y-auto">
        {children}
      </div>
    </div>
  );
}
