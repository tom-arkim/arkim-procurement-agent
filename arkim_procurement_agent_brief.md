# Arkim Procurement Agent — Architectural Brief

**Author:** Tom Dickie, CEO
**Status:** Draft for CTO review
**Scope:** Prototype rebuild against revised strategy

---

## 1. System Overview and Goals

The Arkim Procurement Agent is an AI-driven procurement system for industrial maintenance parts and equipment. It is designed to source, evaluate, and execute parts purchases on behalf of facility operators, with quality and speed that exceeds what a human procurement specialist can deliver.

### Primary goals

- Eliminate the 60–120 day vendor onboarding friction that blocks mid-market industrial buyers from accessing the broader supplier ecosystem.
- Reduce part identification, sourcing, and procurement time from days to minutes for the common case.
- Surface aftermarket alternatives transparently so facilities can capture cost savings without compromising on technical fit or warranty coverage.
- Capture every procurement decision as structured, auditable data that feeds future reliability analytics, supplier scoring, and cost-of-ownership reporting.

### Strategic positioning

The procurement agent is a Series A expansion vector for Arkim. It operates standalone today (manual user-initiated requests) but is designed to integrate with Arkim's Maintenance Assistant platform when that integration becomes available. Both products are sold separately and the procurement agent must function without the maintenance platform being present.

### What this system is not

This is not a chatbot wrapper around a search engine. It is a structured workflow with LLM-driven reasoning at specific decision points. The agent does not have unbounded autonomy — it operates within explicit guardrails (urgency, warranty, approval thresholds) and surfaces decisions to the user at well-defined checkpoints.

---

## 2. The Five-Phase Workflow

Procurement is a durable workflow that may span hours or days from initial request to fulfillment. The system organizes this workflow into five phases, each with explicit entry and exit conditions.

### Phase 1 — Intake and Verification

User initiates a procurement request via chat or image upload. The Intake Agent extracts available data (manufacturer, model, part number, dimensions, materials, electrical specs) and assesses whether the data is sufficient to proceed with sourcing.

**Stop condition:** Manufacturer confidence ≥ 70 AND part identification confidence ≥ 70. Below these thresholds, the agent generates technical follow-up questions (e.g., "I see the model but need the phase voltage to match correctly") and waits for user response. Loop continues until thresholds are met or user explicitly forces "search anyway."

**Output:** Validated AssetSpecs object with confidence-tagged fields, ready to feed downstream agents.

### Phase 2 — Inventory Check (Best-Effort Step 0)

Before searching the external market, the Inventory Agent queries the facility's connected inventory data (when available) to check whether the part is already on site. This is best-effort — if no inventory data is connected, this phase returns "no inventory data available" without blocking the workflow.

**Output:** Either "found at site X with quantity Y" (which surfaces to the user as the recommended path) or "not found / no data" (which proceeds to sourcing).

### Phase 3 — Multi-Tier Sourcing

The Sourcing Agent runs all three tiers in parallel and returns ranked candidates per tier. Tier definitions are defined in Section 6.

**Output:** Three independent result sets (Tier 1, Tier 2, Tier 3), each ranked by TCA (for spare parts) or TLV (for equipment), filtered through the urgency and warranty guardrails (Section 7).

### Phase 4 — Spec Comparison and Verification

For any candidate that is not an exact OEM PN match, the Spec Comparison Agent generates technical context for the engineer to verify fit. The fidelity of this comparison varies by tier (Section 8.4).

**Output:** Per-candidate comparison artifact attached to each surfaced vendor result. The user reviews comparisons before approval.

### Phase 5 — Approval and Procurement

User selects a candidate and initiates approval. The Approval Rules Engine determines required approvers based on dollar threshold and facility configuration. After approval, the Procurement Agent executes the transaction (vendor order, payment, fulfillment tracking, inventory reconciliation, work order linkage).

**Output:** Completed procurement record with full audit trail from initial request through delivery and inventory update.

---

## 3. Agent Definitions and Boundaries

The system uses five specialized agents coordinated by an orchestrator. Agents do not call each other directly — all coordination flows through the orchestrator. This makes failure modes traceable and tests cleaner to write.

### 3.1 Intake Agent

**Responsibility:** Multimodal data extraction, sufficiency assessment, dynamic clarification questioning.

**Inputs:** User chat message, uploaded images (nameplate photos, part photos, assembly photos), prior context from the current ProcurementRun.

**Outputs:** Validated AssetSpecs object with confidence scores per field. Indication of whether to proceed or to ask follow-up question.

**Reasoning model:** Multimodal vision-capable model (Claude Sonnet for image-heavy work, Haiku for text-only follow-ups).

### 3.2 Inventory Agent

**Responsibility:** Query connected inventory systems for the requested part. Best-effort, non-blocking.

**Inputs:** Validated AssetSpecs from Intake.

**Outputs:** Inventory result set (location, quantity, condition) or null if no data available.

**Reasoning model:** Lightweight or none — primarily database query logic. LLM is used only for fuzzy matching when part numbers don't exactly align with inventory records.

### 3.3 Sourcing Agent

**Responsibility:** Run all three tiers of vendor discovery in parallel, apply urgency and warranty filters, return ranked candidates per tier.

**Inputs:** Validated AssetSpecs, urgency setting, warranty status, brand intelligence cache.

**Outputs:** Three ranked result sets (Tier 1, Tier 2, Tier 3), each with vendor metadata, pricing (when available), confidence scores, and audit log entries for filtered candidates.

**Reasoning model:** Claude Haiku for snippet parsing and PN extraction; Tavily for web search.

### 3.4 Spec Comparison Agent

**Responsibility:** Generate technical comparison artifacts for non-exact-match candidates, with fidelity appropriate to data availability.

**Inputs:** AssetSpecs (target part), candidate vendor result, available spec data (from vendor catalog, marketplace listing, or none).

**Outputs:** Comparison artifact with explicit gaps. Fidelity varies:
- High fidelity (Tier 1 onboarded vendors with structured spec data): full side-by-side comparison.
- Medium fidelity (Tier 2 marketplace listings with published specs): comparison of available fields with explicit gaps surfaced.
- Low fidelity (Tier 3 discovered vendors with no spec data): "spec sheet required from vendor before approval" placeholder.

**Reasoning model:** Claude Sonnet for spec analysis where structured data exists; falls back to "request spec sheet" when data is absent.

### 3.5 Procurement Agent

**Responsibility:** Approval workflow management, transaction execution, fulfillment tracking, inventory reconciliation, work order linkage.

**Inputs:** User-selected candidate, current ProcurementRun state, facility approval rules.

**Outputs:** Approval state transitions, vendor purchase order (when integrations exist), fulfillment status updates, inventory updates, completed procurement record.

**Reasoning model:** Minimal — primarily state machine logic. LLM is used for vendor communication drafting (RFQ emails) and for parsing vendor confirmations.

---

## 4. The Orchestrator Pattern

The Orchestrator is a custom Python class (not a framework like LangChain) that coordinates the agents. It owns the ProcurementRun state, calls each agent at the appropriate workflow phase, persists state transitions, and surfaces results to the UI.

### Why custom rather than framework

For Arkim's scale and the level of control required, a custom orchestrator is simpler to debug and easier to evolve than LangChain or LangGraph. The orchestrator pattern matters more than the framework choice. If the system grows to require LangGraph-style stateful agent graphs in the future, the migration path is clean because the agent boundaries are already explicit.

### Orchestrator responsibilities

- Load and persist ProcurementRun state at every transition.
- Determine which agent runs next based on current phase and state.
- Pass structured inputs to agents, validate outputs, handle errors.
- Surface state changes to the UI layer (Streamlit today, replaceable later).
- Write audit log entries for every meaningful decision or transition.

### What the orchestrator is not

The orchestrator is not where business logic lives. It is a coordinator — agents do the actual reasoning and computation. If logic is being added to the orchestrator that isn't pure coordination, it likely belongs in an agent.

---

## 5. ProcurementRun State Model

ProcurementRun is the durable workflow object. Every procurement request is a ProcurementRun with a unique ID, persistent state, and a complete history of transitions.

### Schema (SQLAlchemy ORM, SQLite for prototype, Postgres-ready)

```
ProcurementRun
  id                       UUID primary key
  facility_id              UUID (foreign key to Facility)
  initiated_by_user_id     UUID (foreign key to User; nullable for prototype)
  initiated_at             timestamp
  current_phase            enum [intake, inventory, sourcing, comparison,
                                 pending_first_approval, pending_second_approval,
                                 approved, executing, fulfilling, completed,
                                 cancelled, error]
  asset_specs_json         JSON (the AssetSpecs object as built up during intake)
  urgency_factor           float [0.0–1.0]
  warranty_status          enum [in_warranty, out_of_warranty, warranty_waived, unknown]
  inventory_result_json    JSON (output of Inventory Agent; nullable)
  sourcing_results_json    JSON (output of Sourcing Agent — all three tiers; nullable)
  selected_candidate_json  JSON (the vendor candidate the user approved; nullable)
  approval_history_json    JSON array (each approver's action with timestamp)
  vendor_order_id          string (external order ID once executed; nullable)
  fulfillment_status       enum [pending, shipped, delivered, received, null]
  inventory_update_json    JSON (post-fulfillment inventory record; nullable)
  work_order_link          string (URL/ID for linked work order; nullable)
  audit_log_run_id         UUID (foreign key to AuditLog)
  agent_version            string (the build version that processed this run)
  created_at               timestamp
  updated_at               timestamp
```

### State transitions

State transitions are explicit and persisted. The orchestrator validates that transitions are legal (e.g., cannot move to `executing` from `intake` without passing through `approved`). Invalid transitions throw and are logged as errors.

```
intake → inventory → sourcing → comparison →
  pending_first_approval → [pending_second_approval] → approved →
    executing → fulfilling → completed

Any phase → cancelled (user-initiated)
Any phase → error (system failure; recoverable in some cases)
```

### Why this matters

The state model is the spine of the system. It enables:
- Resumability: a user can leave a procurement run mid-flow and return to it later.
- Multi-step approval: dual approval works because state persists between approver actions.
- Audit completeness: the audit log captures every state transition, not just the final outcome.
- UI flexibility: the Streamlit UI reads current state and renders appropriately; swapping to a React UI later is mechanical.

---

## 6. Tier Definitions (Revised)

The previous prototype's tier definitions were inconsistent with strategic intent. The revised definitions align tiers with Arkim's commercial logic, not with technical implementation details.

### Tier 1 — Arkim Supplier Registry (Onboarded Partners)

Vendors who have been formally onboarded into Arkim's supplier network. They have signed agreements, provided structured catalog and pricing data via API or feed, and can transact through Arkim's merchant-of-record infrastructure.

**Distinguishing characteristics:**
- Real-time pricing and availability via integration.
- Instant checkout — no buyer-side vendor onboarding required.
- Higher commercial margin for Arkim (transaction fees, volume discounts).
- Spec data available for high-fidelity comparison.

**UI presentation:** Surfaced first, distinct visual treatment, "Purchase via Arkim" badge.

### Tier 2 — Digital Marketplace (Public Catalogs)

Vendors with publicly accessible online catalogs and pricing — Grainger, McMaster-Carr, Zoro, MSC, Motion Industries, and similar. Pricing and availability are scraped from their sites in real time. Arkim is not yet onboarded as a buyer with these vendors at the customer level (each customer would need to set up their own account), but Arkim can either (a) facilitate the purchase via the customer's existing account or (b) purchase as Arkim's MoR account and pass through.

**Distinguishing characteristics:**
- Real-time price visibility but no API integration.
- Inventory and lead time visible from listing.
- Active onboarding pipeline: Arkim is working to formally onboard these vendors into Tier 1 over time.
- Spec data available from public listings (medium fidelity).

**UI presentation:** Surfaced second, "Purchase Now" framing, badge indicating "Available immediately, vendor account or Arkim MoR required."

### Tier 3 — Broader Market Discovery

Vendors discovered via open web search who are not in Tier 1 or Tier 2. Includes OEM authorized distributors, aftermarket specialists, regional distributors, and niche suppliers. Pricing and availability typically require quote inquiry rather than online purchase.

**Distinguishing characteristics:**
- Discovery-based — Tavily search anchored on manufacturer, part type, and authorized distributor terms.
- OEM authorized distributors prioritized within Tier 3 (badge applied based on brand intelligence data and URL pattern matching).
- Quote-required workflow — Arkim contacts vendor on user's behalf within 24–48 hours.
- Spec data minimal — comparison agent surfaces "spec sheet required" placeholder.

**UI presentation:** Surfaced third, "Quote Required" framing, OEM Authorized Distributor badge where applicable.

### Tier handoff logic

All three tiers run in parallel. Each tier returns its own ranked result set. The user sees all three sets and can act on any of them. There is no automatic handoff that hides results — the principle is full transparency, with the user choosing which tier to act from based on their priorities (speed, cost, vendor preference, transaction friction tolerance).

---

## 7. Urgency and Warranty Guardrails

These are orthogonal axes that affect ranking and filtering, not separate sourcing paths.

### Urgency settings

Three discrete settings (no continuous slider — see Section 11 on UI principles):

**Stocking** (urgency_factor 0.0): Cost-optimized. Lead time flexible. Lowest-priced candidates rank highest. Used for routine replenishment when no operational pressure exists.

**Predictive** (urgency_factor 0.3): Balanced. Moderate weight on lead time, moderate weight on cost. Default setting. Used when failure is anticipated within 30 days based on Arkim's diagnostic signal (or user judgment).

**Emergency** (urgency_factor 1.0): Speed-optimized. Lead time dominates ranking. Cost variance up to a configurable threshold is acceptable. Used when equipment is currently down.

The TCA scoring formula incorporates urgency_factor as a ranking input, not a filtering input. All vendors surface regardless of urgency setting; their relative ranking shifts.

### Warranty guardrails

Three states with explicit behavior:

**In Warranty:** Tier 3 aftermarket results are filtered out entirely. Only OEM-authentic vendors (OEM direct, OEM authorized distributors, exact OEM PN matches) surface. If no OEM channel results found, the system surfaces a banner and routes to Tier 3 RFQ outreach to OEM direct.

**Out of Warranty / Warranty Waived:** All tiers and match types surface. Aftermarket equivalents are visible alongside OEM. The user makes the final call.

**Unknown:** All results surface but a prominent banner warns that aftermarket parts may void warranty coverage. The user is prompted to confirm warranty status with the asset owner before approval.

---

## 8. Detailed Component Specifications

### 8.1 Intake Agent — Sufficiency Logic

The agent maintains a per-field confidence score for AssetSpecs fields. Sufficiency is determined by a combination of:

- Manufacturer confidence ≥ 70 (computed from how the manufacturer was identified — visible on nameplate, stated by user, or pattern-inferred from part number).
- Part identification confidence ≥ 70 (computed from how the part type and category were identified).
- Category-specific required fields populated. For motors: voltage, phase, frame. For seals: shaft size, material. For VFDs: voltage, current rating. For sensors: signal type, process connection size. The required field list is per-category and lives in a config file.

When sufficiency is not met, the agent identifies the most informative missing field and asks one focused question. It does not chain multiple questions — one question per turn keeps the conversation natural.

Force-proceed escape: the user may respond "search anyway" to bypass sufficiency requirements. The agent proceeds with degraded confidence, and downstream filters apply more conservatively (e.g., higher confidence floor, narrower interchange tolerance).

### 8.2 Inventory Agent — Connection Patterns

For prototype: a configuration table per facility specifies whether inventory data is connected, and if so, via what method. Three patterns supported:

- No connection: returns null immediately.
- CSV upload: facility uploads inventory file periodically; agent queries the file.
- API integration: facility has connected CMMS or ERP; agent queries via API.

Production will add real-time integrations with Maximo, eMaint, Fiix, UpKeep, NetSuite, and similar systems. For prototype, CSV upload with periodic refresh is sufficient.

### 8.3 Sourcing Agent — Tier Execution

Each tier is a separate code path with its own query construction, vendor scoring, and result ranking. They share the underlying scoring functions (suitability, confidence, counterfeit risk, home field bonus) but apply them differently based on tier characteristics.

Tier 1 query: direct API calls to onboarded vendors (when integrations exist). For prototype, this is mocked — a hardcoded list of "onboarded vendors" with sample availability and pricing.

Tier 2 query: Tavily search restricted to known marketplace domains, with structured snippet parsing for price and availability.

Tier 3 query: Tavily unrestricted search with manufacturer-anchored query construction, brand intelligence integration, OEM authorized distributor detection.

All three tiers write to the same audit log with tier-tagged entries.

### 8.4 Spec Comparison Agent — Fidelity Tiers

Three comparison fidelities based on data availability:

**High fidelity** (Tier 1 onboarded vendors): Structured spec sheet from the vendor's catalog. The agent generates a side-by-side comparison covering all critical dimensions (mechanical, electrical, material, certification). Output: visual comparison table with highlighted matches and mismatches.

**Medium fidelity** (Tier 2 marketplace listings): Spec data scraped from the vendor's product page. The agent extracts what's available, compares to AssetSpecs, and explicitly surfaces gaps. Output: partial comparison table with "verify with vendor" tags on incomplete fields.

**Low fidelity** (Tier 3 discovered vendors): No spec data available. The agent generates a placeholder: "Spec sheet required from vendor before approval. The vendor's catalog page is at [URL] — contact them via Arkim's quote request workflow to obtain detailed specs." The engineer is responsible for verifying technical fit before approval.

The comparison agent's output is always honest about its fidelity. It does not pretend to verify what it cannot verify. This is core to the engineer's trust in the system.

### 8.5 Approval Rules Engine

Configuration-driven approval routing. A facility-level config defines rules:

```
{
  "facility_id": "uuid",
  "rules": [
    {
      "threshold_usd": 0,
      "approvers_required": 1,
      "approver_roles": ["maintenance_director", "operations_manager", "vp_operations"]
    },
    {
      "threshold_usd": 5000,
      "approvers_required": 2,
      "approver_roles": ["maintenance_director", "operations_manager"]
    },
    {
      "threshold_usd": 25000,
      "approvers_required": 2,
      "approver_roles": ["operations_manager", "vp_operations"]
    }
  ]
}
```

The orchestrator, on entering the approval phase, reads the rules, determines the applicable rule based on the candidate's grand total, and sets the required approvals. State transitions through `pending_first_approval` and (if required) `pending_second_approval` before reaching `approved`.

For prototype: rules are editable via a Streamlit admin page (gated behind `SHOW_ADMIN_VIEW`). Approver roles are display labels only — no actual user/role enforcement until production identity infrastructure exists.

For production: rules move to a database table with proper user/role lookup, RBAC enforcement, and notification flow (email or in-app) to required approvers.

---

## 9. Integration Contract with Maintenance Assistant

The procurement agent must operate standalone today but be ready to accept input from the Maintenance Assistant when that system exists. The integration contract is defined now even though the Maintenance Assistant is not yet built.

### Input contract

The Maintenance Assistant will produce a structured payload when its diagnostic engine determines a part is needed:

```
{
  "request_source": "maintenance_assistant",
  "diagnostic_event_id": "uuid",
  "facility_id": "uuid",
  "asset_id": "uuid",
  "asset_specs": {
    "manufacturer": "...",
    "model": "...",
    "part_number": "...",
    "detected_type": "...",
    "category": "Part" | "Equipment",
    "required_fields": { ... }
  },
  "urgency_factor": 1.0,
  "warranty_status": "in_warranty",
  "failure_mode": "...",
  "operator_notes": "..."
}
```

The procurement agent's entry point accepts this payload as an alternative to user-initiated chat input. The orchestrator detects the `request_source` field and routes accordingly. From the perspective of all downstream agents, the input is the same — a populated AssetSpecs object with associated context.

### Output contract

When a procurement run completes, the agent emits a structured event back to the Maintenance Assistant:

```
{
  "procurement_run_id": "uuid",
  "diagnostic_event_id": "uuid",
  "status": "completed" | "cancelled" | "error",
  "fulfillment_eta": "iso8601",
  "total_cost_usd": 0.0,
  "vendor_used": "...",
  "spec_match_fidelity": "high" | "medium" | "low",
  "linked_work_order_id": "..."
}
```

The Maintenance Assistant uses this to update the work order, schedule installation, and update its own asset history.

### Why design this contract now

The contract design is a one-day exercise today and a one-week refactor later. By defining the input and output schemas now and validating the procurement agent against them in unit tests, the future integration is a connection rather than a rebuild.

---

## 10. What's Preserved from the Current Codebase

The current implementation has 11 modules in `utils/sourcing/` plus supporting infrastructure. The rebuild keeps the modules whose responsibilities are unchanged and rebuilds the ones whose semantics are shifting.

### Preserved without modification

- `scoring.py` — suitability, confidence, home field, counterfeit penalty scoring functions.
- `filtering.py` — warranty gate, registry enrichment, confidence floor, category mismatch guard.
- `price_sanity.py` — tiered thresholds, extreme outlier filter, single-price market reference.
- `constants.py` — threshold values, category lists, marketplace domains.
- `llm_parsing.py` — Anthropic HTTP wrapper, snippet parsing, batch XML extraction.
- `market_confidence.py` — Tavily + Claude reliability scoring.
- `vendor_tokens.py` — UUID4 token generation, partner onboarding URL construction.
- `utils/audit_log.py` — schema and write infrastructure.
- `utils/supplier_registry.py` — schema and lookup logic.
- `utils/brand_intelligence.py` — LLM-driven manufacturer relationship cache.

### Preserved with modifications

- `utils/models.py` — extend AssetSpecs with new confidence fields; add ProcurementRun model.
- `tier3_outreach.py` — refactor into the broader Sourcing Agent rather than a separate path.

### Replaced or rebuilt

- `orchestrator.py` — replace with the new Orchestrator class managing ProcurementRun state.
- `enterprise_search.py` — split into Tier 1, Tier 2, Tier 3 specific code paths with new tier semantics.
- `tavily_client.py` — query construction needs to align with new tier definitions.
- `chat_app.py` — Streamlit UI rebuild around the workflow phase model.

### Built new

- ProcurementRun SQLAlchemy model and persistence layer.
- The five agent classes (Intake, Inventory, Sourcing, Spec Comparison, Procurement).
- Custom Orchestrator class with state transition logic.
- Spec Comparison Agent with fidelity-tier handling.
- Approval Rules Engine with config-driven routing.
- Inventory Agent with CSV upload and API connection patterns.
- Maintenance Assistant input/output contract validation.

---

## 11. UI and User Experience Principles

The Streamlit UI is the prototype interface. Production will replace it with a proper frontend, but the UI principles remain.

### Workflow-first rendering

The UI displays the current ProcurementRun state and renders appropriate controls for the current phase. During Intake, the user sees a chat interface and can upload images. During Sourcing, the user sees the three-tier results. During Approval, the user sees the candidate selection and approval flow. The UI does not show all phases at once.

### Discrete controls over continuous controls

For the urgency setting, the UI uses three discrete buttons (Stocking, Predictive, Emergency), not a slider. Discrete controls are easier to demo, easier to explain, and produce more visibly distinct outputs. Sliders imply gradation that confuses non-technical users.

### Tooltips for non-obvious concepts

Every non-obvious control has a tooltip explaining what it does. Urgency, warranty status, tier definitions, confidence scores, and counterfeit risk flags all have hover-text explanations. The tooltip burden is one sentence per concept.

### Honest communication of system state

The UI tells the user what the system did and what it didn't do. If a tier returned zero results, the UI says so. If spec data is unavailable for a candidate, the UI labels it as such. If the agent is uncertain, the UI surfaces the uncertainty rather than masking it. Trust is built by transparency, not polish.

---

## 12. Out-of-Scope for Prototype

The following are deliberately deferred to post-seed work. They are flagged here so the prototype does not accidentally creep into them.

- Real merchant-of-record infrastructure (payment processing, tax compliance, supplier contracts). Prototype simulates execution without actual payment flow.
- Actual outbound email sending. Email templates are generated and previewed only. `EMAIL_SEND_ENABLED = False` is enforced.
- Production-grade identity, authentication, and RBAC. Approval rules use display-label roles without enforcement.
- Multi-tenant data isolation. Prototype assumes single-customer context.
- Real-time inventory integrations with Maximo, Fiix, eMaint, etc. Prototype supports CSV upload only.
- Vendor API integrations for Tier 1 onboarded suppliers. Tier 1 is mocked with hardcoded sample data.
- Production observability (Datadog, Sentry, structured log aggregation).
- Comprehensive automated test coverage. Prototype includes targeted tests on pure functions (scoring, comparison logic) but not end-to-end integration tests.
- Replacement of Streamlit with a production frontend. Streamlit is the prototype UI substrate.

---

## 13. Implementation Sequence

The rebuild is structured in phases that produce working software at each step. The prototype is functional after Phase 2 and progressively richer through Phase 5.

### Phase 1 — Foundation (Week 1)

- ProcurementRun SQLAlchemy model and persistence.
- Orchestrator class skeleton with state transition logic.
- Stub agent classes with contracts defined but minimal implementations.
- Streamlit UI skeleton showing current phase and state.
- Existing modules (scoring, filtering, audit log, brand intelligence) wired into the new structure.

**Definition of done:** A ProcurementRun can be created, persists to SQLite, and transitions through phases (with stubbed agent behavior). UI renders the current state.

### Phase 2 — Intake and Sourcing (Week 2)

- Intake Agent with multimodal extraction, sufficiency logic, dynamic questioning.
- Sourcing Agent with all three tiers running in parallel.
- New tier definitions and query construction for Tier 1 (mocked), Tier 2, Tier 3.
- UI rendering of three-tier results with appropriate badges.

**Definition of done:** A user can submit a request via chat or image upload, the agent verifies sufficiency and asks follow-ups when needed, and three tiers of results return correctly.

### Phase 3 — Comparison and Approval (Week 3)

- Spec Comparison Agent with three-fidelity output.
- Approval Rules Engine with admin UI for rule editing.
- ProcurementRun state transitions for approval flow.
- UI rendering of comparison artifacts and approval workflow.

**Definition of done:** A user can review surfaced candidates with appropriate-fidelity comparisons, initiate approval, and see the dual approval flow when triggered by dollar threshold rules.

### Phase 4 — Execution and Reconciliation (Week 4)

- Procurement Agent with simulated execution (no real payment).
- Fulfillment tracking states.
- Inventory reconciliation logic with chat-driven location capture.
- Work order linkage (placeholder until Maintenance Assistant integration).

**Definition of done:** An approved candidate proceeds through a simulated procurement flow with all state transitions captured in the audit log.

### Phase 5 — Inventory Agent and Polish (Week 5, optional)

- Inventory Agent with CSV upload and basic API stub.
- Step 0 inventory check integrated into orchestrator.
- UI polish, error handling improvements, end-to-end testing of full flow.

**Definition of done:** The full five-phase workflow runs end-to-end against representative test cases (mechanical seal, motor, sensor) with appropriate behavior at each stage.

---

## 14. Risks and Open Questions

### Risks

**Architectural drift during implementation.** The previous iteration accumulated patches that diverged from intent. Mitigation: this brief is the source of truth. Implementation decisions reference back to it; updates to the brief are deliberate.

**Streamlit UI limitations becoming blockers.** Streamlit is acceptable for prototype but the workflow-first rendering may strain its capabilities. Mitigation: keep the UI thin, with all logic in the orchestrator and agents. Streamlit becomes a presentation layer that's straightforward to replace.

**LLM cost inflation as the agent matures.** Five agents each making LLM calls per ProcurementRun could become expensive at scale. Mitigation: explicit cost tracking from day one (existing infrastructure), per-agent cost budgets, cheaper models (Haiku) for routine tasks, more capable models (Sonnet) only where reasoning quality matters.

**Brand intelligence cache cold-start problem.** New manufacturers require LLM lookups that are slow on first encounter. Mitigation: pre-warm the cache for the most common industrial manufacturers; tolerate the 5-second latency on cold lookups.

### Open questions for CTO review

1. Should the Orchestrator be a class or a state machine library? A custom class is simpler but a state machine library (e.g., `transitions`) provides validated state transitions and visualization for free.

2. Where should ProcurementRun state changes emit events? Adding an event bus pattern now (even with a simple in-process implementation) would make future integrations cleaner. Worth adding now or defer?

3. Should the agent boundaries be enforced via Python protocols/abstract base classes, or kept as plain classes with conventional contracts? More structure helps testability but adds boilerplate.

4. The Spec Comparison Agent's medium-fidelity output requires scraping marketplace listings. Tavily's snippet content is often insufficient for full spec extraction. Should the Sourcing Agent fetch full page content for top candidates, or should the Comparison Agent be responsible for that on demand?

5. Is the four-week implementation sequence realistic for the current engineering capacity? Adjustments to scope or sequencing welcome.

---

## Appendix A — Glossary

- **AssetSpecs:** The structured representation of the part or equipment being sourced. Includes manufacturer, model, part number, dimensions, electrical specs, material, and confidence scores per field.
- **ProcurementRun:** The durable workflow object representing one procurement request from initiation to completion.
- **TCA (Total Cost of Acquisition):** Ranking score for spare parts. Weighted combination of speed, reliability, cost efficiency, and friction.
- **TLV (Total Life Cycle Value):** Ranking score for equipment. Includes urgency-factor-adjusted lead time impact on operations.
- **Tier 1 / 2 / 3:** Sourcing tiers defined by Arkim's commercial relationship with the vendor (onboarded / public marketplace / discovered).
- **Brand Intelligence:** LLM-driven cache of manufacturer relationships, parent companies, authorized distributors, and category-specific terminology.
- **Counterfeit Risk Categories:** Industrial part categories (bearings, seals, semiconductors, fasteners) where counterfeit supply is a known problem and authorized sourcing is preferred.
- **OEM Authorized Distributor:** A vendor with an explicit channel relationship to the original equipment manufacturer, eligible for warranty preservation.
