import { ProcShell } from "@/components/proc/proc-shell";

/** Customer run-scoped screens (Options, later Quotes/Order) share the proc shell. */
export default function PartsLayout({ children }: { children: React.ReactNode }) {
  return <ProcShell>{children}</ProcShell>;
}
