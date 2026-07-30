import { Suspense } from "react";
import { RequestScreen } from "@/components/proc/request-screen";

/** Customer "I need a part" — describe → identify → confirm → find options.
 *  Suspense boundary: RequestScreen reads search params (?prefill= from the
 *  reorder affordance, ?resume= from the home triage queue). */
export default function RequestPage() {
  return (
    <Suspense fallback={null}>
      <RequestScreen />
    </Suspense>
  );
}
