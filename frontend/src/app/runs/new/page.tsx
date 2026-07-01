"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useCreateRun, useFacilities } from "@/lib/queries";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "@/components/ui/icons";
import { cn } from "@/lib/utils";

const URGENCY_OPTIONS = [
  {
    value: 0.2,
    label: "Stocking",
    desc: "Planned replenishment, flexible lead time",
    tone: "ghost" as const,
  },
  {
    value: 0.6,
    label: "Predictive",
    desc: "Asset showing signs of wear, days to weeks",
    tone: "amber" as const,
  },
  {
    value: 0.95,
    label: "Emergency",
    desc: "Asset down or critical failure imminent",
    tone: "red" as const,
  },
];

const WARRANTY_OPTIONS = [
  { value: "unknown", label: "Unknown" },
  { value: "active", label: "Active" },
  { value: "expired", label: "Expired" },
];

const toneClass = {
  ghost: "border-hr-3 bg-bg-3 text-fg-2 hover:border-hr-4 hover:bg-bg-4",
  amber: "border-amber-line bg-amber-tint text-amber-fg",
  red: "border-red-line bg-red-tint text-red-fg",
};

export default function NewRunPage() {
  const router = useRouter();
  const { data: facilities } = useFacilities();
  const createRun = useCreateRun();

  const [facilityId, setFacilityId] = useState("");
  const [urgencyFactor, setUrgencyFactor] = useState(0.2);
  const [warranty, setWarranty] = useState("unknown");

  const handleSubmit = () => {
    createRun.mutate(
      {
        facility_id: facilityId || undefined,
        urgency_factor: urgencyFactor,
        warranty_status: warranty,
      },
      {
        onSuccess: (data) => router.push(`/runs/${data.id}`),
      },
    );
  };

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      <div className="shrink-0 border-b border-hr-2 px-5 py-3">
        <h1 className="text-h2 text-fg-1">New sourcing run</h1>
        <p className="font-mono text-[10.5px] text-fg-4 mt-0.5">
          Configure the run context, then describe the part in the intake chat.
        </p>
      </div>

      <div className="flex-1 px-5 py-6 max-w-[560px] flex flex-col gap-6">
        {/* Facility */}
        <FormField label="Facility">
          <select
            className="w-full rounded border border-hr-3 bg-bg-3 px-3 py-2 text-sm text-fg-1 focus:border-blue-line focus:outline-none"
            value={facilityId}
            onChange={(e) => setFacilityId(e.target.value)}
          >
            <option value="">— Select facility —</option>
            {facilities?.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name} · {f.state}
              </option>
            ))}
          </select>
        </FormField>

        {/* Urgency */}
        <FormField label="Urgency">
          <div className="grid grid-cols-3 gap-2">
            {URGENCY_OPTIONS.map((opt) => (
              <button
                key={opt.label}
                onClick={() => setUrgencyFactor(opt.value)}
                className={cn(
                  "rounded-card border p-3 text-left transition-colors",
                  urgencyFactor === opt.value
                    ? toneClass[opt.tone]
                    : "border-hr-2 bg-bg-2 text-fg-3 hover:bg-bg-3",
                )}
              >
                <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.08em]">
                  {opt.label}
                </p>
                <p className="mt-1 text-[11px] text-fg-3 leading-snug">{opt.desc}</p>
              </button>
            ))}
          </div>
        </FormField>

        {/* Warranty */}
        <FormField label="Warranty status">
          <div className="flex gap-2">
            {WARRANTY_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                onClick={() => setWarranty(opt.value)}
                className={cn(
                  "flex-1 rounded border px-3 py-2 font-mono text-[11px] uppercase tracking-[0.08em] transition-colors",
                  warranty === opt.value
                    ? "border-blue-line bg-blue-tint text-blue-fg"
                    : "border-hr-2 bg-bg-2 text-fg-3 hover:bg-bg-3",
                )}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </FormField>

        {/* Actions */}
        <div className="flex items-center gap-3 pt-2">
          <Link href="/runs">
            <Button variant="ghost" size="md">
              Cancel
            </Button>
          </Link>
          <Button
            variant="primary"
            size="md"
            trailingIcon={<ArrowRight size={13} />}
            loading={createRun.isPending}
            onClick={handleSubmit}
          >
            Start run
          </Button>
        </div>

        {createRun.isError && (
          <p className="text-sm text-red-fg">
            Failed to create run — is the backend running on port 8000?
          </p>
        )}
      </div>
    </div>
  );
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-2">
      <label className="font-mono text-[10.5px] uppercase tracking-[0.10em] text-fg-3">
        {label}
      </label>
      {children}
    </div>
  );
}
