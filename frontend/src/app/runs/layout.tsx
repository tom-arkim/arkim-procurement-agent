import { AppLayout } from "@/components/arkim/app-layout";
import { SidebarNav } from "@/components/arkim/sidebar-nav";

export default function RunsLayout({ children }: { children: React.ReactNode }) {
  return (
    <AppLayout sidebar={<SidebarNav />}>
      {children}
    </AppLayout>
  );
}
