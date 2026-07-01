"use client";

import { Button } from "@/components/ui/button";
import { Send } from "@/components/ui/icons";
import { useArkimStore, selectSelectedCount } from "@/store";
import { useInitiateOutreach } from "@/lib/queries";

interface StickyActionBarProps {
  runId: string;
}

export function StickyActionBar({ runId }: StickyActionBarProps) {
  const count = useArkimStore(selectSelectedCount(runId));
  const selection = useArkimStore((s) => s.tier3Selection[runId] ?? new Set<string>());
  const pushToast = useArkimStore((s) => s.pushToast);
  const clearTier3Selection = useArkimStore((s) => s.clearTier3Selection);
  const outreach = useInitiateOutreach(runId);

  if (count === 0) return null;

  const candidateIds = Array.from(selection);

  const handleSend = () => {
    outreach.mutate(
      { candidate_ids: candidateIds },
      {
        onSuccess: () => {
          clearTier3Selection(runId);
          pushToast({
            tone: "green",
            head: "Outreach sent",
            sub: `Contacted ${count} vendor${count !== 1 ? "s" : ""}`,
          });
        },
        onError: () =>
          pushToast({
            tone: "amber",
            head: "Outreach failed",
            sub: "Check backend connection and retry.",
          }),
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

      <div className="flex flex-col items-center gap-0.5">
        <Button
          variant="primary"
          size="sm"
          loading={outreach.isPending}
          onClick={handleSend}
          className="flex items-center gap-1.5"
        >
          <Send size={13} />
          Confirm outreach
        </Button>
        <span className="font-mono text-[8.5px] uppercase tracking-[0.08em] text-fg-4">
          On your behalf · Arkim sends, you receive
        </span>
      </div>
    </div>
  );
}
