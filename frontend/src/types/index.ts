/**
 * Arkim Sourcing Engine — TypeScript types
 *
 * Derived from /design/data-model.md. Kept in sync manually; any field added
 * to data-model.md should be reflected here and in the FastAPI Pydantic models.
 */

// ---------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------

export type Phase =
  | "pending_intake"
  | "intake"
  | "inventory"
  | "sourcing"
  | "comparison"
  | "pending_first_approval"
  | "pending_second_approval"
  | "approved"
  | "executing"
  | "fulfilling"
  | "completed"
  | "cancelled"
  | "error";

export type Urgency = "Stocking" | "Predictive" | "Emergency";
export type Warranty = "Active" | "Expired" | "Unknown";

export type PnMatchLevel = "exact" | "normalized" | "stem" | "substring" | "none";
export type VendorType =
  | "NetworkPartner"
  | "NationalDistributor"
  | "AftermarketCompatible"
  | "AuthorizedDistributor"
  | "RegionalSpecialist"
  | "IndustrialSurplus";

export type ComparisonFidelity = "high" | "medium" | "low";
export type CompatibilitySummary =
  | "fit_confirmed"
  | "fit_likely"
  | "verification_required"
  | "incompatible";
export type FieldMatch = "exact" | "compatible" | "different" | "unknown";
export type CompletionKind =
  | "arkim_transaction"
  | "external_handoff"
  | "cancelled"
  | "no_results";
export type ApprovalAction = "approved" | "rejected";
export type AppliesTo = "buy" | "outreach";

// ---------------------------------------------------------------------------
// Core domain types
// ---------------------------------------------------------------------------

export interface AssetSpecs {
  manufacturer: string;
  model: string;
  part_number: string;
  description?: string;
  category: "Equipment" | "Part";
  detected_type?: string;
  // Confidence fields
  manufacturer_confidence: number;
  part_id_confidence?: number;
  // Per-category fields (pump / motor / VFD / bearing)
  hp?: string;
  rpm?: string;
  voltage?: string;
  frame?: string;
  shaft_size?: string;
  bore_diameter?: string;
  impeller_size?: string;
  mech_seal?: string;
  material_spec?: string;
  gpm?: string;
  psi?: string;
  phase?: string;
  enclosure?: string;
  protocol?: string;
  seal?: string;
  // Lifecycle context
  urgency_factor: number;
  warranty_status?: string;
  failure_mode?: string;
  asset_id?: string;
  diagnostic_event_id?: string;
  // True when sufficiency reached without a model or part number (spec-based sourcing path).
  // Set by the backend; drives "By spec" label in the confirm-card secondary fields.
  spec_based_sourcing?: boolean;
}



export interface FieldComparison {
  field: string;
  fieldLabel: string;
  assetValue?: string;
  candidateValue?: string;
  match: FieldMatch;
  notes?: string;
}

export interface ComparisonArtifact {
  fidelity: ComparisonFidelity;
  compatibilitySummary: CompatibilitySummary;
  comparison: FieldComparison[];
  verificationRequiredFields: string[];
  engineerNotes?: string;
}

export interface Candidate {
  id: string;
  vendorName: string;
  vendorType: VendorType;
  tier: 1 | 2 | 3;
  price?: number;
  // increment 1 ("priced"/"uncontacted"); "quoted" = State C (increment 3): a
  // human-confirmed RFQ quote — the strongest claim in the ladder.
  evidenceState?: "priced" | "uncontacted" | "quoted";
  purchaseChannel?: "marketplace" | "reference";  // increment 2 (State M); marketplace = buyable price
  // Null when no real lead time exists yet (a pre-quote/RFQ row or absent data) — render
  // "Lead time on quote", never a fabricated number. leadTimeSource is the provenance,
  // mirroring price's priceVerified/evidenceState: a "defaulted" value is shown but qualified
  // (estimated); "extracted"/"quoted" are real.
  leadTime: string | null;
  leadTimeSource?: "extracted" | "defaulted" | "placeholder" | "quoted";
  url: string;
  suitability: number;
  confidence: number;
  pnMatchLevel: PnMatchLevel;
  comparisonArtifact?: ComparisonArtifact;
  loc: string;
  // Display-layer extras
  foundPartNumber?: string;                    // listing's actual PN (priced rows)
  isExactMatch?: boolean;
  isAftermarket?: boolean;
  isOemDirect?: boolean;
  isAuthorizedDistributor?: boolean;
  stock?: string;
  shipFrom?: string;
  priceVerified?: boolean;
  priceUnverified?: boolean;                   // extracted price below the confidence floor
  // State C (increment 3, "quoted"): a human-confirmed RFQ quote overlaid on the candidate.
  quoteConfirmed?: boolean;                    // a confirmed quote drives the display
  quoteUnverified?: boolean;                   // the quote's extraction confidence was below the 0–1 floor
  terms?: string;                              // quote payment/shipping terms, when stated
  quoteCurrency?: string;                      // quote currency (default USD)
  contact?: string;
  relationship?: string;
  // Tier 1 two-mode display: true = show "Request Confirmation"; false = show "Buy Now".
  confirmationPending?: boolean;
}

export interface ApprovalActionRecord {
  sequence: 1 | 2 | 3;
  approver_role: string;
  approver_name?: string;
  action: ApprovalAction;
  notes?: string;
  acted_at: string;
}

export interface CompletionEvent {
  kind: CompletionKind;
  at: string;
  vendor?: string;
  vendorUrl?: string;
  transactionId?: string;
  amount?: number;
  notes?: string;
}

export interface Facility {
  id: string;
  name: string;
  state: string;
  approvalRules?: ApprovalRule[];
}

export interface ApprovalRule {
  id: string;
  facility_id: string;
  threshold: number;
  cap?: number;
  approvers_required: number;   // tiers may be edited (incl. 0 = auto-approve)
  approver_roles: string[];
  applies_to: AppliesTo;
}

// ---------------------------------------------------------------------------
// Sourcing run — API response shapes
// ---------------------------------------------------------------------------

export interface SourcingRunListItem {
  id: string;
  phase: Phase;
  urgency: Urgency;
  warranty: Warranty;
  facility_id: string;
  asset_summary?: string;
  amount?: number;
  maintenance_submission_id?: string;
  created_at: string;
  updated_at: string;
}

export interface SourcingResults {
  tier1: Candidate[];
  tier2: Candidate[];
  tier3: Candidate[];
  warrantyBanner?: string;
  tier3CapabilityPivot?: boolean;
}

export interface SourcingRunDetail {
  id: string;
  phase: Phase;
  urgency: Urgency;
  warranty: Warranty;
  facility_id: string;
  facility_state: string;
  /** Basket label — runs sharing one group_id form a basket. Null/absent on single-part runs.
   *  Already sent by the backend (RunDetail); drives the basket status strip. */
  group_id?: string | null;
  asset_specs?: AssetSpecs;
  inventory_result?: Record<string, unknown>;
  sourcing_results?: SourcingResults;
  selected_candidate?: Candidate;
  approval_history: ApprovalActionRecord[];
  completion_event?: CompletionEvent;
  tier3_selection?: string[];
  /** candidateId → sentAt ISO — set after POST /outreach fires. Drives OutreachCard "Awaiting" state. */
  tier3_outreach_sent?: Record<string, string>;
  maintenance_handoff?: Record<string, unknown>;
  messages?: ChatMessage[];
  /** True when T2+T3 have candidates but none are an exact PN match. Drives transparency banner. */
  no_exact_match?: boolean;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// Chat message (intake agent thread)
// ---------------------------------------------------------------------------

export interface ChatMessage {
  id: string;
  role: "user" | "agent" | "system";
  content: string;
  created_at: string;
  attachment?: {
    type: "image";
    filename: string;
    size_bytes: number;
    /** Local object URL for optimistic preview — only present on client-generated messages, never from server. */
    previewUrl?: string;
  };
}

// ---------------------------------------------------------------------------
// API request / response shapes
// ---------------------------------------------------------------------------

/** One row of the basket rollup (GET /api/groups/{gid}) — mirrors the backend BasketRunRow. */
export interface BasketRunRow {
  run_id: string | null;
  part: string | null;            // real intake label or honest placeholder; null only on a degraded row
  phase: string | null;
  selected_amount: number;        // 0.0 until a candidate is selected (never faked)
  error: string | null;           // set when the row degraded (fail-soft), else null
}

/** Basket rollup (GET /api/groups/{gid}) — mirrors the backend BasketRollup. */
export interface BasketRollup {
  group_id: string;
  status: string;
  basket_total: number;
  run_count: number;
  runs: BasketRunRow[];
}

export interface CreateRunRequest {
  facility_id?: string;
  urgency_factor?: number;
  warranty_status?: string;
  /** Optional basket label — runs sharing one group_id form a basket. Omitted -> group-less. */
  group_id?: string;
  /** Optional pre-extracted specs to seed the run at birth (multi-part fan-out — no re-extraction). */
  asset_specs?: Record<string, unknown>;
}

export interface CreateRunResponse {
  id: string;
  phase: Phase;
  created_at: string;
}

export interface SendMessageRequest {
  content: string;
  role?: "user";
}

export interface SendMessageResponse {
  run_id: string;
  message: {
    id: string;
    role: "agent";
    content: string;
    created_at: string;
  };
  updated_phase: Phase;
  /** Intake sufficiency state (e.g. "multi_part_detected"); null on single-part responses. */
  proceed_state?: string | null;
  /** When proceed_state === "multi_part_detected", the N parsed per-part specs (to fan out). */
  parts?: Record<string, unknown>[] | null;
}

export interface SelectCandidateRequest {
  candidate_id: string;
  tier: 1 | 2 | 3;
}

export interface ApproveRequest {
  approver_name: string;
  approver_role: string;
  notes?: string;
}

export interface RejectRequest {
  approver_name: string;
  approver_role: string;
  notes: string;
}

export interface OutreachRequest {
  candidate_ids: string[];
}

// ---------------------------------------------------------------------------
// Buyer loop — inbound quote review (comparison table) + order placement
// ---------------------------------------------------------------------------

export type ReviewItemKind = "quote" | "contact";
export type ReviewItemStatus =
  | "pending" | "needs_human_review" | "confirmed" | "rejected"
  // operator dismiss of an unmatched_reply (distinct from "rejected" = discarded quote/contact)
  | "dismissed";

export interface ReviewItem {
  id: string;
  kind: ReviewItemKind;
  status: ReviewItemStatus;
  run_id?: string | null;
  supplier_domain?: string | null;
  vendor_name?: string | null;
  manufacturer?: string | null;
  part_number?: string | null;
  /** 0–1 from the extractor; multiply by 100 for the ConfidenceIndicator. */
  confidence?: number | null;
  raw_source?: string | null;
  created_at?: string;
  /** quote: unit_price/currency/quantity/lead_time/min_order/terms; contact: name/email/position. */
  payload: {
    unit_price?: number | null;
    currency?: string;
    quantity?: number | null;
    lead_time?: string | null;
    min_order?: number | null;
    terms?: string | null;
    name?: string | null;
    email?: string | null;
    position?: string | null;
  };
}

export interface ReviewItemsResponse {
  run_id: string;
  review_items: ReviewItem[];
  /** RFQs sent for this run — drives partial state ("2 of 3 suppliers responded"). */
  sent_count: number;
  quote_count: number;
}

export interface ProcessRepliesResponse {
  run_id: string;
  available: boolean;
  summary: {
    processed: number;
    queued_quotes: number;
    queued_contacts: number;
    needs_review: number;
    unmatched: string[];
  } | null;
  queued_for_run?: number;
  message?: string;
}

export interface ConfirmReviewItemResponse {
  item_id: string;
  kind: ReviewItemKind;
  confirmed: boolean;
  item: ReviewItem | null;
}

export interface RejectReviewItemResponse {
  item_id: string;
  rejected: boolean;
  item: ReviewItem | null;
}

export type OrderStatus =
  // pending_manual_fulfilment = an approved order awaiting an operator to buy/source it
  // (manual marketplace fulfilment) before it advances to placed.
  | "draft" | "pending_manual_fulfilment" | "placed" | "confirmed" | "shipped" | "received" | "cancelled";

export interface Order {
  id: string;
  run_id?: string | null;
  manufacturer?: string | null;
  part_number?: string | null;
  vendor_name?: string | null;
  supplier_domain?: string | null;
  unit_price?: number | null;
  currency?: string | null;
  quantity?: number | null;
  lead_time?: string | null;
  source?: "buy" | "rfq" | null;
  status: OrderStatus;
  created_at?: string;
  updated_at?: string;
  placed_by?: string | null;
}

/** Result of execute / mark-delivered (ProcurementAgent action result). */
export interface OrderActionResult {
  success: boolean;
  action: string;
  order: Order | null;
  placed?: boolean;
  message?: string;
  next_phase?: string | null;
}

export interface OrdersResponse {
  run_id: string;
  count: number;
  orders: Order[];
}

// ---------------------------------------------------------------------------
// "Your Arkim impact" — output of utils/impact.py (GET /api/impact).
// Savings are MEASURED, counts are COUNTED, time is an ESTIMATE (labelled with its
// model version). The UI renders these; it never recomputes the arithmetic.
// ---------------------------------------------------------------------------

export interface ImpactCounts {
  parts_identified: number;
  suppliers_contacted: number;
  quotes_read: number;
  comparisons_made: number;
  replies_chased: number;
}

export interface ImpactMonth {
  month: string; // "YYYY-MM" — real order months only, never interpolated
  savings: number; // a real 0 stays 0 (no comparable purchase that month)
  order_ids: string[];
  note: string;
}

/** One order's measured saving — drillable per-order proof. */
export interface ImpactBreakdownItem {
  order_id: string | null;
  month: string;
  saving: number;
  saving_basis: string | null;
  part?: string | null;
  vendor?: string | null;
}

// ---------------------------------------------------------------------------
// Reorder intelligence — forecast from the customer's own order cadence.
// ---------------------------------------------------------------------------

export interface ReorderItem {
  manufacturer?: string | null;
  part_number: string;
  part: string;
  vendor_name?: string | null;
  order_count: number;
  avg_interval_days: number;
  avg_interval_weeks: number;
  last_ordered: string;
  days_since: number;
  next_due: string;
  days_until: number;
  status: "ok" | "due_soon" | "overdue";
  note: string;
}

export interface CumulativeImpact {
  total_savings: number;
  savings_by_month: ImpactMonth[];
  counts: ImpactCounts;
  time_estimate_minutes: number;
  estimate_model_version: string;
  contributing_order_ids: string[];
  breakdown: ImpactBreakdownItem[];
}

// ---------------------------------------------------------------------------
// Derived notification feed (GET /api/events) — read-only, untargeted, real-state.
// Shaped by api_server.py _derive_events() from existing rows (order statuses, run
// approval phase/history, confirmed quotes); there is NO notifications table. Untargeted:
// no verified per-user identity exists yet, so events span all runs and never claim a
// specific person was notified. Every event reflects the actual current row.
// ---------------------------------------------------------------------------

export type EventType = "order_status" | "approval" | "quote_confirmed";

export interface EventItem {
  id: string;
  type: EventType;
  run_id?: string | null;
  order_id?: string | null;
  title: string;
  timestamp?: string | null; // real updated_at / acted_at / resolved_at (ISO-8601 UTC)
}

export interface EventsResponse {
  count: number;
  events: EventItem[];
}

// ---------------------------------------------------------------------------
// UI-layer helpers
// ---------------------------------------------------------------------------

/** Derived display urgency for RunBar pills */
export function urgencyTone(u: Urgency): "red" | "amber" | "ghost" {
  if (u === "Emergency") return "red";
  if (u === "Predictive") return "amber";
  return "ghost";
}

/** Phase → design system label (matches design canvas Phase component) */
export const PHASE_LABELS: Record<Phase, string> = {
  pending_intake: "Maintenance",
  intake: "Intake",
  inventory: "Inventory",
  sourcing: "Sourcing",
  comparison: "Comparison",
  pending_first_approval: "Approval",
  pending_second_approval: "Approval",
  approved: "Approved",
  executing: "Executing",
  fulfilling: "Fulfilling",
  completed: "Completed",
  cancelled: "Cancelled",
  error: "Error",
};

/** Five steps shown in the Phase progress bar */
export const PHASE_STEPS = ["Intake", "Sourcing", "Comparison", "Approval", "Completed"] as const;
export type PhaseStep = (typeof PHASE_STEPS)[number];

export function phaseToStep(phase: Phase): PhaseStep {
  const map: Record<Phase, PhaseStep> = {
    pending_intake: "Intake",
    intake: "Intake",
    inventory: "Intake",
    sourcing: "Sourcing",
    comparison: "Comparison",
    pending_first_approval: "Approval",
    pending_second_approval: "Approval",
    approved: "Approval",
    executing: "Approval",
    fulfilling: "Completed",
    completed: "Completed",
    cancelled: "Completed",
    error: "Intake",
  };
  return map[phase] ?? "Intake";
}
