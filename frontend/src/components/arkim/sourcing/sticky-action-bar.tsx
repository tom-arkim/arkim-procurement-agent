"use client";

import { Button } from "@/components/ui/button";
import { Send } from "@/components/ui/icons";
import { useArkimStore, selectSelectedCount } from "@/store";
import { useInitiateOutreach, useSaveOutreach } from "@/lib/queries";

interface StickyActionBarProps {
  runId: string;
}

export function StickyActionBar({ runId }: StickyActionBarProps) {
  const count = useArkimStore(selectSelectedCount(runId));
  const selection = useArkimStore((s) => s.tier3Selection[runId] ?? new Set<string>());
  const pushToast = useArkimStore((s) => s.pushToast);

  const outreach = useInitiateOutreach(runId);
  const save = useSaveOutreach(runId);

  const vendorNames = Array.from(selection);
  const disabled = count === 0;

  const handleSave = () => {
    save.mutate(vendorNames, {
      onSuccess: () => pushToast({ tone: "green", head: "Selection saved" }),
    });
  };

  const handleSend = () => {
    outreach.mutate(
      { vendor_names: vendorNames },
      {
        onSuccess: () =>
          pushToast({ tone: "green", head: "Outreach sent", sub: `Contacted ${count} vendor${count !== 1 ? "s" : ""}` }),
        onError: () =>
          pushToast({ tone: "amber", head: "Outreach failed", sub: "Check backend connection and retry." }),
      },
    );
  };

  return (
    <div className="sticky bottom-0 z-10 border-t border-hr-2 bg-bg-1 px-4 py-3 flex items-center gap-3">
      <span className="font-mono text-[11px] text-fg-3 shrink-0">
        <span className="text-fg-1 font-semibold">{count}</span>
        {" "}vendor{count !== 1 ? "s" : ""} selected
      </span>

      <div className="flex-1" />

      <Button
        variant="ghost"
        size="sm"
        disabled={disabled}
        onClick={() =>
          pushToast({ tone: "blue", head: "Preview drafts", sub: "Draft preview coming in Phase 5." })
        }
      >
        Preview drafts
      </Button>

      <Button
        variant="secondary"
        size="sm"
        disabled={disabled}
        loading={save.isPending}
        onClick={handleSave}
      >
        Save selection
      </Button>

      <div className="flex flex-col items-center gap-0.5">
        <Button
          variant="primary"
          size="sm"
          disabled={disabled}
          loading={outreach.isPending}
          onClick={handleSend}
          className="flex items-center gap-1.5"
        >
          <Send size={13} />
          Send outreach
        </Button>
        <span className="font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-4">
          On your behalf · Arkim sends, you receive
        </span>
      </div>
    </div>
  );
}
