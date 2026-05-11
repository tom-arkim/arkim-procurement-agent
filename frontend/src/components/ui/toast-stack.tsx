"use client";

import { useCallback, useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { X, CheckCircle, Warn } from "@/components/ui/icons";
import { useArkimStore } from "@/store";
import type { Toast, ToastTone } from "@/store";

const AUTO_DISMISS_MS = 4000;
const MAX_VISIBLE = 5;

export function ToastStack() {
  const toasts = useArkimStore((s) => s.toasts);
  const dismissToast = useArkimStore((s) => s.dismissToast);

  // Evict oldest when queue exceeds max
  useEffect(() => {
    if (toasts.length > MAX_VISIBLE) {
      dismissToast(toasts[0].id);
    }
  }, [toasts, dismissToast]);

  return (
    <div
      className="fixed bottom-4 right-4 z-[200] flex flex-col-reverse gap-2 w-80 max-sm:inset-x-4 max-sm:w-auto"
      aria-live="polite"
      aria-label="Notifications"
    >
      {toasts.slice(-MAX_VISIBLE).map((toast) => (
        <ToastItem key={toast.id} toast={toast} />
      ))}
    </div>
  );
}

const ACCENT: Record<ToastTone, string> = {
  blue:  "border-l-blue-line",
  green: "border-l-green-line",
  amber: "border-l-amber-line",
};

const ICON_COLOR: Record<ToastTone, string> = {
  blue:  "text-blue-fg",
  green: "text-green-fg",
  amber: "text-amber-fg",
};

function ToastItem({ toast }: { toast: Toast }) {
  const dismissToast = useArkimStore((s) => s.dismissToast);
  const [leaving, setLeaving] = useState(false);

  const dismiss = useCallback(() => {
    setLeaving(true);
    setTimeout(() => dismissToast(toast.id), 200);
  }, [toast.id, dismissToast]);

  // Auto-dismiss timer — depends only on stable values so the timer never resets
  useEffect(() => {
    if (toast.sticky) return;
    const timer = setTimeout(dismiss, AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [toast.id, toast.sticky, dismiss]);

  return (
    <div
      className={cn(
        "flex items-start gap-3 rounded-card border border-hr-2 bg-bg-3 p-3 shadow-card",
        "border-l-2", ACCENT[toast.tone],
        leaving
          ? "animate-out slide-out-to-right-4 fade-out duration-200"
          : "animate-in slide-in-from-right-4 fade-in duration-200",
      )}
    >
      <span className={cn("shrink-0 mt-0.5", ICON_COLOR[toast.tone])}>
        {toast.tone === "amber" ? <Warn size={15} /> : <CheckCircle size={15} />}
      </span>

      <div className="flex flex-col gap-0.5 flex-1 min-w-0">
        <p className="text-sm font-semibold text-fg-1 leading-snug">{toast.head}</p>
        {toast.sub && (
          <p className="font-mono text-[11px] text-fg-3 leading-snug">{toast.sub}</p>
        )}
      </div>

      <button
        onClick={dismiss}
        className="shrink-0 mt-0.5 text-fg-4 hover:text-fg-2 transition-colors"
        aria-label="Dismiss notification"
      >
        <X size={13} />
      </button>
    </div>
  );
}
