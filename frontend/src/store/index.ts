/**
 * Zustand client state store.
 *
 * Only state that is NOT server state lives here:
 *  - UI panels (expanded, collapsed)
 *  - Intake chat draft text
 *  - Tier 3 vendor selection (pre-send, not yet persisted to backend)
 *  - Toast notifications
 *
 * Server state (run data, facilities, rules) lives in TanStack Query.
 */

import { create } from "zustand";
import { immer } from "zustand/middleware/immer";
import { enableMapSet } from "immer";
import type { Phase } from "@/types";

// Required for Immer to track Set.add / Set.delete mutations inside drafts.
enableMapSet();

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

export type ToastTone = "blue" | "green" | "amber";

export interface Toast {
  id: string;
  tone: ToastTone;
  head: string;
  sub?: string;
  /** Sticky toasts (e.g. connection-lost) never auto-dismiss. */
  sticky?: boolean;
}

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

interface GoferState {
  // --- Asset spec panel ---
  assetSpecsExpanded: boolean;
  toggleAssetSpecs: () => void;

  // --- Intake chat draft ---
  chatDraft: string;
  setChatDraft: (text: string) => void;

  // --- Tier 3 vendor selection (ephemeral, per active run) ---
  /** runId → set of selected candidate IDs */
  tier3Selection: Record<string, Set<string>>;
  toggleTier3Vendor: (runId: string, candidateId: string) => void;
  setTier3Selection: (runId: string, candidateIds: string[]) => void;
  clearTier3Selection: (runId: string) => void;

  // --- Tier 1 confirmation sent timestamps (ephemeral, survives page nav but not refresh) ---
  /** runId → candidateId → ISO sentAt. Set when "Request Confirmation" is clicked. */
  tier1ConfirmSentAt: Record<string, Record<string, string>>;
  markTier1ConfirmSent: (runId: string, candidateId: string, sentAt: string) => void;

  // --- Toast notifications ---
  toasts: Toast[];
  pushToast: (toast: Omit<Toast, "id">) => void;
  dismissToast: (id: string) => void;

  // --- Phase transition tracking (drives toast banners) ---
  lastKnownPhase: Record<string, Phase>;
  setLastKnownPhase: (runId: string, phase: Phase) => void;

  // --- Sidebar nav ---
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useGoferStore = create<GoferState>()(
  immer((set) => ({
    // Asset specs panel
    assetSpecsExpanded: true,
    toggleAssetSpecs: () =>
      set((s) => {
        s.assetSpecsExpanded = !s.assetSpecsExpanded;
      }),

    // Intake chat draft
    chatDraft: "",
    setChatDraft: (text) =>
      set((s) => {
        s.chatDraft = text;
      }),

    // Tier 3 selection
    tier3Selection: {},
    toggleTier3Vendor: (runId, candidateId) =>
      set((s) => {
        if (!s.tier3Selection[runId]) {
          s.tier3Selection[runId] = new Set();
        }
        const sel = s.tier3Selection[runId];
        if (sel.has(candidateId)) {
          sel.delete(candidateId);
        } else {
          sel.add(candidateId);
        }
      }),
    setTier3Selection: (runId, candidateIds) =>
      set((s) => {
        s.tier3Selection[runId] = new Set(candidateIds);
      }),
    clearTier3Selection: (runId) =>
      set((s) => {
        delete s.tier3Selection[runId];
      }),

    // Tier 1 confirmation sent timestamps
    tier1ConfirmSentAt: {},
    markTier1ConfirmSent: (runId, candidateId, sentAt) =>
      set((s) => {
        if (!s.tier1ConfirmSentAt[runId]) s.tier1ConfirmSentAt[runId] = {};
        s.tier1ConfirmSentAt[runId][candidateId] = sentAt;
      }),

    // Toasts
    toasts: [],
    pushToast: (toast) =>
      set((s) => {
        const id = Math.random().toString(36).slice(2);
        s.toasts.push({ ...toast, id });
      }),
    dismissToast: (id) =>
      set((s) => {
        s.toasts = s.toasts.filter((t: Toast) => t.id !== id);
      }),

    // Phase tracking
    lastKnownPhase: {},
    setLastKnownPhase: (runId, phase) =>
      set((s) => {
        s.lastKnownPhase[runId] = phase;
      }),

    // Sidebar
    sidebarOpen: true,
    setSidebarOpen: (open) =>
      set((s) => {
        s.sidebarOpen = open;
      }),
  })),
);

// ---------------------------------------------------------------------------
// Convenience selectors
// ---------------------------------------------------------------------------

export const selectTier3Selection = (runId: string) => (s: GoferState) =>
  s.tier3Selection[runId] ?? new Set<string>();

export const selectSelectedCount = (runId: string) => (s: GoferState) =>
  (s.tier3Selection[runId] ?? new Set()).size;
