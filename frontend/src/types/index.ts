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
  evidenceState?: "priced" | "uncontacted";   // increment 1; "quoted" is a later increment
  purchaseChannel?: "marketplace" | "reference";  // increment 2 (State M); marketplace = buyable price
  leadTime: string;
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
  approvers_required: 1 | 2 | 3;
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

export interface CreateRunRequest {
  facility_id?: string;
  urgency_factor?: number;
  warranty_status?: string;
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
export type ReviewItemStatus = "pending" | "needs_human_review" | "confirmed" | "rejected";

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
  | "draft" | "placed" | "confirmed" | "shipped" | "received" | "cancelled";

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
