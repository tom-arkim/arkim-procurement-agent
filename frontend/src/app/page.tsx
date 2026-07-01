import { ProcShell } from "@/components/proc/proc-shell";
import { HomeScreen } from "@/components/proc/home-screen";

/**
 * Root `/` — the customer "Parts & Orders" Home ("What needs me").
 * The internal Sourcing-Engine app lives at /runs and /admin (its own mono/blue shell);
 * this customer surface uses the ported Figma design system (proc-theme).
 */
export default function HomePage() {
  return (
    <ProcShell>
      <HomeScreen />
    </ProcShell>
  );
}
