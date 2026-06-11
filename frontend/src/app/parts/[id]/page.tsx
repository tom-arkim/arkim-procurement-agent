"use client";

import { use } from "react";
import { OptionsScreen } from "@/components/proc/options-screen";

/** Customer view of a sourcing run — "Here are your best options". */
export default function PartOptionsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <OptionsScreen runId={id} />;
}
