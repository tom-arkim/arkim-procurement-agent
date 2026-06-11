import { ProcShell } from "@/components/proc/proc-shell";

export default function RequestLayout({ children }: { children: React.ReactNode }) {
  return <ProcShell>{children}</ProcShell>;
}
