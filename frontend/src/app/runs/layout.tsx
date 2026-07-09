import { AppLayout } from "@/components/gofer/app-layout";
import { SidebarNav } from "@/components/gofer/sidebar-nav";

export default function RunsLayout({ children }: { children: React.ReactNode }) {
  return (
    <AppLayout sidebar={<SidebarNav />}>
      {children}
    </AppLayout>
  );
}
