"use client";

/**
 * RequestScreen — "I need a part" (frontend spec §2 / proc-request.jsx), ported to the
 * proc design and wired to the REAL intake: create run → IntakeAgent chat (extract
 * asset_specs) → confirm → start sourcing → Options.
 *
 * Plain language, honest about uncertainty: describe in plain words, the agent
 * identifies the part (or asks a follow-up), you confirm or add detail, then we find
 * options. No procurement jargon. Spec extraction + sufficiency is the backend's; the
 * UI never fabricates an identification.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { createRun, sendMessage, confirmIntake, uploadNameplate, seedAssetSpecs } from "@/lib/api";
import { useRun } from "@/lib/queries";
import { queryKeys, apiErrorMessage, ApiError } from "@/lib/query-client";
import { ProcIcon } from "./proc-icon";
import { ProcHead } from "./proc-ui";
import { GoferLoader } from "@/components/ui/gofer-loader";
import { useProcToast } from "./proc-shell";
import type { AssetSpecs } from "@/types";

const QUICK = [
  "Filter for the main compressor",
  "V-belt for the transfer belt",
  "Door seal for the tray washer",
];

const NULLS = new Set(["", "n/a", "null", "none", "unknown-pn", "unknown", "tbd"]);
const val = (v?: string | null) => (v && !NULLS.has(String(v).toLowerCase().trim()) ? String(v) : undefined);

// Variant-selecting attr -> real AssetSpecs fields (mirrors the backend
// part_type_registry.VARIANT_ATTR_TO_SPEC_FIELDS). Used to derive whether a
// family-variant 422 block is GENUINELY satisfied from the refetched specs —
// the `_variant_disambig_pending` flag the backend guard also reads is STRIPPED
// from frontend asset_specs (api_server._orm_to_detail drops `_`-prefixed keys),
// so satisfaction is derived from the real spec fields the 422's missing_attrs
// map to. Keep in sync with the backend mapping when a variant-selecting attr is
// added (the suite asserts a mapping row exists for every variant_selecting_attr).
const VARIANT_ATTR_FIELDS: Record<string, readonly string[]> = {
  hp: ["hp"],
  voltage_phase: ["voltage", "phase"],
  shaft_size: ["shaft_size"],
  bore_diameter: ["bore_diameter"],
  hydraulic_duty: ["gpm", "psi", "head", "hp"],
};
// Null/placeholder values the backend treats as unanswered (part_type_registry.
// variant_attr_answered). Case-sensitive, no "n/a" — mirrors the backend set
// exactly so the derived check matches the guard's notion of "answered".
const BACKEND_NULL = new Set(["", "null", "N/A", "Unknown", "UNKNOWN-PN", "none", "unknown"]);

// Part-type acronyms that must stay fully upper-cased in a display title (VFD, PLC…).
// Sentence-case would render these wrong ("Vfd"), and they ARE real part classes.
const TYPE_ACRONYMS = new Set(["vfd", "plc", "hmi", "vsd", "ups", "scr", "ac", "dc"]);
// Title-case a detected_type for the headline: acronyms upper-cased, otherwise
// sentence case ("mechanical seal" -> "Mechanical seal", "vfd" -> "VFD").
const formatType = (t: string): string =>
  t.split(/\s+/)
    .map((w, i) =>
      TYPE_ACRONYMS.has(w.toLowerCase())
        ? w.toUpperCase()
        : i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w,
    )
    .join(" ");

function specsReady(specs?: AssetSpecs): boolean {
  if (!specs) return false;
  // Spec-based commit (proceed_spec_based / forced_commit) is a deliberate backend "ready"
  // signal — the intake committed to source by category with NO manufacturer/model/PN. The
  // mfg && identity gate below would short-circuit on the missing manufacturer and never
  // honor it, so recognize it first. The "Matching by category" meta line renders in the
  // card to qualify this as identified-by-spec, not by identity.
  if (specs.spec_based_sourcing) return true;
  const mfg = val(specs.manufacturer);
  return Boolean(mfg && (val(specs.part_number) || val(specs.model)));
}

type Stage = "entry" | "working" | "identify" | "error";

// The family-variant 422 detail (T5 contract — test_run_pending_then_confirm_422).
// Sent by api_server.confirm_intake when a family-level request for a variant-
// selecting class is confirmed without a resolved variant-selecting attr. `pending`
// distinguishes "confirm the rating you've nearly named" from "provide the rating".
type FamilyVariantBlock = {
  message: string;
  model: string;
  missing_attrs: string[];
  missing_labels: string[];
  pending: boolean;
};

/** One part in the request, each backed by its OWN run (the per-item / basket model). The
 *  parent holds the runId + the latest intake reply; specs / partLabel / ready-state are
 *  derived per-card from useRun(runId) (Stage A data layer). */
type IntakeItem = { runId: string; reply: string; multiPart?: boolean };

export function RequestScreen() {
  const router = useRouter();
  const qc = useQueryClient();
  const fire = useProcToast();
  const params = useSearchParams();

  const [stage, setStage] = useState<Stage>("entry");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  // Photo dropzone: the entry card accepts a dragged image (same path as the
  // attach button — one file; the upload endpoint takes a single nameplate shot).
  const [dragOver, setDragOver] = useState(false);
  // Each described part is its OWN run; all share ONE client-minted basket group_id, so the
  // resolved list proceeds to sourcing as a single basket. Every item renders as a uniform
  // ItemCard (item 0 included).
  const [items, setItems] = useState<IntakeItem[]>([]);
  const [groupId, setGroupId] = useState<string | null>(null);
  // The count of parts an auto-split fan-out created (null = not a fan-out) — drives the
  // "We found N parts…" review instruction. Manual + Add part leaves it null.
  const [autoSplitCount, setAutoSplitCount] = useState<number | null>(null);
  // "+ add another part" inline input.
  const [addText, setAddText] = useState("");
  const [addBusy, setAddBusy] = useState(false);
  const addRef = useRef<HTMLInputElement>(null);
  // Per-item ready state reported UP by each card (the parent can't call useRun in a loop), so
  // the basket advance can gate on GENUINE all-sufficient.
  const [readyById, setReadyById] = useState<Record<string, boolean>>({});
  const [advancing, setAdvancing] = useState(false);
  // Family-variant 422 detail attributed to the specific run that confirm_intake
  // blocked (T5). Keyed by runId so only the blocked card re-surfaces the ask;
  // cleared at the start of each advance so a fresh confirm never shows a stale block.
  const [blockById, setBlockById] = useState<Record<string, FamilyVariantBlock>>({});
  // The real failure reason (server detail) when the backend responded with an error;
  // null means a pure network failure -> show the "is the backend running?" fallback.
  const [errMsg, setErrMsg] = useState<string | null>(null);

  const refresh = (id: string) => {
    qc.invalidateQueries({ queryKey: queryKeys.runs.detail(id) });
    qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
  };

  // A card reports its ready state; the guard avoids churning renders on an unchanged value.
  const handleReady = useCallback((rid: string, r: boolean) => {
    setReadyById((prev) => (prev[rid] === r ? prev : { ...prev, [rid]: r }));
  }, []);

  const allReady = items.length > 0 && items.every((it) => readyById[it.runId] === true);
  const readyCount = items.filter((it) => readyById[it.runId] === true).length;

  // Deep-link entries (frontend-only, no new endpoints):
  //  ?prefill=<text>  — the reorder affordance seeds the description box.
  //  ?resume=<runId>  — the home triage queue reopens a run stuck mid-intake as
  //    an identify card (single-part: it already has its own group at birth, so
  //    "+ add part" is hidden on a resume — no cross-basket mixing).
  const prefill = params.get("prefill");
  const resumeId = params.get("resume");
  const resumed = Boolean(resumeId);
  useEffect(() => {
    if (prefill) setText((t) => t || prefill);
  }, [prefill]);
  useEffect(() => {
    if (resumeId) {
      setItems((prev) => (prev.length ? prev : [{ runId: resumeId, reply: "" }]));
      setStage((s) => (s === "entry" ? "identify" : s));
    }
  }, [resumeId]);

  // A returned part is seedable only if it carries REAL identity — never seed a card from an
  // empty/identity-less fragment (no invented parts).
  const isSeedablePart = (p: Record<string, unknown>): boolean =>
    Boolean(
      val(p.manufacturer as string) || val(p.model as string) || val(p.part_number as string) ||
      val(p.description as string) || val(p.detected_type as string),
    );

  // Multi-part auto-split: reuse run 0 as part 1 (seed parts[0] onto it via PUT — it already
  // carries the basket group_id, so no orphan, no re-extraction) and birth-seed parts 2..N as
  // their own runs in the SAME basket. N pre-populated cards, each showing exactly what was detected.
  const fanOut = async (run0Id: string, gid: string, parts: Record<string, unknown>[]) => {
    await seedAssetSpecs(run0Id, parts[0]);
    const rest = await Promise.all(
      parts.slice(1).map((p) => createRun({ group_id: gid, asset_specs: p })),
    );
    const newItems: IntakeItem[] = [
      { runId: run0Id, reply: "" },
      ...rest.map((r) => ({ runId: r.id, reply: "" })),
    ];
    setItems(newItems);
    setAutoSplitCount(parts.length);   // drives the "We found N parts…" review instruction
    newItems.forEach((it) => refresh(it.runId));
  };

  // Remove a card (e.g. a wrongly-split part). The removed run lingers harmlessly in intake —
  // the basket advance only confirms runs in the list, so a removed one is never advanced.
  const removeItem = (runId: string) => {
    setItems((prev) => prev.filter((it) => it.runId !== runId));
    setReadyById((prev) => {
      const next = { ...prev };
      delete next[runId];
      return next;
    });
  };

  const start = async () => {
    const desc = text.trim();
    if (!desc && !file) return;
    setStage("working");
    setAutoSplitCount(null);   // fresh submission — only a fan-out sets it
    try {
      // Mint the basket group_id up front; item 0's run is created INTO it, as is every add.
      const gid = crypto.randomUUID();
      setGroupId(gid);
      const created = await createRun({ group_id: gid });
      // Nameplate photo -> vision extraction (with the typed text alongside, so the image never
      // discards the description); else text intake. Both responses carry proceed_state + parts.
      const resp = file
        ? await uploadNameplate(created.id, file, desc)
        : await sendMessage(created.id, { content: desc });
      const itemReply = resp.message?.content ?? "Read the nameplate from your photo.";

      // Auto-split: a multi-part detection fans the N parsed parts into N seeded cards. Fall
      // back to the single card (with the "N detected, + Add part" reply) if fewer than 2 parts
      // carry real content — never create empty/guessed cards.
      const multiPartDetected = resp.proceed_state === "multi_part_detected";
      const detected = multiPartDetected ? (resp.parts ?? []) : [];
      const seedable = detected.filter(isSeedablePart);
      if (seedable.length >= 2) {
        await fanOut(created.id, gid, seedable);
        setStage("identify");
        return;
      }

      // Fallback: multi-part was detected but couldn't be seeded (<2 real parts) -> flag the
      // single card so its heading reads "Multiple parts detected", not "Unidentified part".
      setItems([{ runId: created.id, reply: itemReply, multiPart: multiPartDetected }]);
      refresh(created.id);
      setStage("identify");
    } catch (e) {
      setErrMsg(apiErrorMessage(e));   // server detail (B/C) or null for a network failure (A)
      setStage("error");
    }
  };

  // "+ add another part" — each added part is its own run created INTO the same basket group.
  const addPart = async () => {
    const desc = addText.trim();
    if (!desc || !groupId) return;
    setAddBusy(true);
    try {
      const created = await createRun({ group_id: groupId });
      const r = await sendMessage(created.id, { content: desc });
      setItems((prev) => [...prev, { runId: created.id, reply: r.message.content }]);
      refresh(created.id);
      setAddText("");
    } catch {
      fire("Couldn't add that part — please try again.");
    } finally {
      setAddBusy(false);
    }
  };

  // The visible "+ Add part" CTA: with text it adds (same path as Enter); with an EMPTY box it
  // focuses the input so the click is never a dead no-op. (addBusy still blocks a double-add.)
  const handleAddClick = () => {
    if (!addText.trim()) {
      addRef.current?.focus();
      return;
    }
    addPart();
  };

  // ONE basket action: confirm EVERY item's intake (each enters sourcing under the shared
  // group_id — the whole basket advances), then open the options view. Gated on genuine
  // all-sufficient, so no unresolved item is ever silently dropped or assumed ready.
  const advance = async () => {
    if (!allReady || advancing || items.length === 0) return;
    setAdvancing(true);
    setBlockById({});   // fresh confirm — never carry a stale block forward
    // allSettled (not Promise.all) so a 422 on one item doesn't short-circuit and
    // hide which runs DID advance to sourcing. Per-run attribution lets the exact
    // blocked card re-surface the family-variant ask (T5); other rejections keep the
    // existing toast. The variant-blocked items render state on their card (no toast).
    const results = await Promise.allSettled(items.map((it) => confirmIntake(it.runId, false)));
    const blocked: Record<string, FamilyVariantBlock> = {};
    let failed = false;
    results.forEach((r, i) => {
      if (r.status === "fulfilled") return;
      if (r.reason instanceof ApiError && r.reason.status === 422) {
        const detail = (r.reason.body as { detail?: unknown } | undefined)?.detail;
        if (detail && typeof detail === "object" && (detail as { reason?: string }).reason === "family_variant_unconfirmed") {
          blocked[items[i].runId] = detail as FamilyVariantBlock;
          return;
        }
      }
      failed = true;
    });
    if (failed) fire("Couldn't start sourcing — please try again.");
    if (Object.keys(blocked).length) setBlockById(blocked);
    if (!failed && !Object.keys(blocked).length) {
      router.push(`/parts/${items[0].runId}`);
    }
    setAdvancing(false);   // stay on the page so the user can answer / escape / retry
  };

  return (
    <div className="proc-max proc-center">
      <ProcHead
        title={<>I need <b>a part</b></>}
        sub="Describe it in plain words — we'll identify it and find your best options."
      />

      {/* ENTRY */}
      {stage === "entry" && (
        <>
          <div
            className="proc-ask"
            data-drag={dragOver || undefined}
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              const dropped = Array.from(e.dataTransfer.files).find((f) => f.type.startsWith("image/"));
              if (dropped) setFile(dropped);
            }}
          >
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); start(); } }}
              placeholder="Describe the part you need… (e.g. 'air-oil separator for the GA37 compressor')"
              autoFocus
            />
            {file && (
              <div className="ask-file">
                <ProcIcon name="toolbox" size={16} color="var(--muted)" />
                <span>
                  <div>{file.name}</div>
                  <div className="af-meta">Nameplate photo · {Math.round(file.size / 1024)} KB</div>
                </span>
                <button className="af-x" onClick={() => setFile(null)} title="Remove"><ProcIcon name="chevR" size={14} /></button>
              </div>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              style={{ display: "none" }}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
            <div className="ask-row">
              <button className="ask-attach" onClick={() => fileRef.current?.click()}>
                <ProcIcon name="box" size={15} />Photo of the nameplate
              </button>
              <span className="ask-spacer" />
              <button className="ask-send" disabled={!text.trim() && !file} onClick={start}>
                <ProcIcon name="arrowR" size={17} color="var(--on-accent)" />
              </button>
            </div>
          </div>
          {!text && (
            <div className="ask-chips">
              {QUICK.map((q) => (
                <button key={q} className="ask-chip" onClick={() => setText(q)}>
                  <ProcIcon name="spark" size={13} />{q}
                </button>
              ))}
            </div>
          )}
          <div className="ask-hint">
            <ProcIcon name="alert" size={13} color="var(--muted-2)" />
            No part number? That&apos;s fine — describe what it does, snap the nameplate,
            or drag a photo in.
          </div>
        </>
      )}

      {/* WORKING */}
      {stage === "working" && (
        <div className="proc-loading">
          <GoferLoader size={52} />
          <div className="pl-head">{file ? "Reading the nameplate" : "Identifying part"}</div>
          <div className="pl-sub">{file ? "Pulling the part details from your photo." : "Checking service records and manuals."}</div>
        </div>
      )}

      {/* ERROR */}
      {stage === "error" && (
        <div className="proc-working">
          <ProcIcon name="alert" size={20} color="var(--st-overdue)" />
          <div>
            <div className="w-t">{errMsg ?? "Couldn't process that request."}</div>
            <div className="w-s">{errMsg ? null : <>Is the backend running? </>}<button className="proc-btn" data-kind="quiet" style={{ padding: "2px 6px" }} onClick={() => { setErrMsg(null); setStage("entry"); }}>Try again</button></div>
          </div>
        </div>
      )}

      {/* IDENTIFY — every part is a uniform card that resolves independently; ONE basket advance. */}
      {stage === "identify" && (
        <div style={{ marginTop: 8 }}>
          <div className="proc-kicker">{items.length > 1 ? "Your parts" : "Your part"}</div>

          {/* Auto-split only: sets the expectation + points at the remove control. */}
          {autoSplitCount != null && items.length > 1 && (
            <div className="id-meta" style={{ marginBottom: 4, color: "var(--muted)" }}>
              We found {autoSplitCount} parts — review each below, and remove any we split by mistake.
            </div>
          )}

          {items.map((item) => (
            <ItemCard
              key={item.runId}
              runId={item.runId}
              initialReply={item.reply}
              multiPart={item.multiPart}
              variantBlock={blockById[item.runId]}
              onReady={handleReady}
              // Removable only when there's more than one card — you can't remove the last part.
              onRemove={items.length > 1 ? removeItem : undefined}
            />
          ))}

          {/* + add another part — a new run created into the SAME basket group. The button and
              Enter share one path (handleAddClick / addPart); an empty box focuses, never no-ops.
              Hidden on a ?resume= deep link (no basket group to add into). */}
          {!resumed && (
            <div className="id-actions" style={{ marginTop: 12 }}>
              <input
                ref={addRef}
                className="proc-idinput"
                value={addText}
                onChange={(e) => setAddText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") addPart(); }}
                placeholder="Add another part — describe it…"
              />
              <button className="proc-btn" data-kind="quiet" disabled={addBusy} onClick={handleAddClick}>
                {addBusy ? "Adding…" : "+ Add part"}
              </button>
            </div>
          )}

          {/* Basket advance — ONE action for the whole group, enabled only when EVERY item is sufficient. */}
          <div className="id-actions" style={{ marginTop: 16 }}>
            <button className="proc-btn" data-kind="primary" disabled={!allReady || advancing} onClick={advance}>
              <ProcIcon name="checkCircle" size={15} />
              {advancing
                ? "Finding options…"
                : items.length > 1 ? `Find options for ${items.length} parts` : "Find options"}
            </button>
          </div>
          {!allReady && (
            <div className="id-meta" style={{ marginTop: 8, color: "var(--muted)" }}>
              {readyCount} of {items.length} {items.length === 1 ? "part" : "parts"} identified — add the
              missing details on the {readyCount === items.length - 1 ? "card" : "cards"} above to continue.
            </div>
          )}
        </div>
      )}
    </div>
  );
}


/** Per-item card: one part's identity + state, and — only when it's under-specified — its OWN
 *  clarification input. Owns its useRun (hooks can't loop) and its own clarification state, so
 *  each card resolves independently: one card's submit/pending/error never touches another's.
 *
 *  - partLabel comes from the run's REAL intake result, "Unidentified part" if absent (never guessed).
 *  - A ready item (specsReady true, incl. a PN'd part via the over-ask fix) shows NO box.
 *  - While its request is pending the input AND button lock together; they re-enable on response
 *    OR error. An error surfaces the real detail on THIS card (server detail, or the connectivity
 *    line for a true network failure) and the user can edit + retry.
 *  - Reports its ready state UP via onReady so the parent can gate the basket advance on
 *    GENUINE all-sufficient (the parent can't call useRun in a loop).
 *  - onRemove (when provided) renders a remove control so a wrongly-split part can be dropped
 *    without affecting the others (the raised-stakes guard for auto-split). */
function ItemCard({
  runId,
  initialReply,
  multiPart,
  variantBlock,
  onReady,
  onRemove,
}: {
  runId: string;
  initialReply: string;
  multiPart?: boolean;
  variantBlock?: FamilyVariantBlock;
  onReady?: (runId: string, ready: boolean) => void;
  onRemove?: (runId: string) => void;
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const fire = useProcToast();
  const { data: run } = useRun(runId, { enabled: Boolean(runId) });
  const specs = run?.asset_specs;
  const ready = specsReady(specs);
  const pn = val(specs?.part_number);
  const mfg = val(specs?.manufacturer);
  // Parent identity (mfg + model/PN) — how the headline has always read.
  const parentId = [mfg, val(specs?.model) || pn].filter(Boolean).join(" ");
  // Lead with the component when the identified item is a PART of a parent asset
  // (category "Part" + a detected_type), so a seal for a Goulds 3196 reads as the
  // seal — not the pump. Equipment (the unit itself) keeps its plain identity, and
  // seeded/untyped specs are unchanged: the seeded demo carries no category or
  // detected_type, so flag-off cards are untouched. (Both come from base
  // extraction; the gate is the data, not the flag.) The full spec fallback is
  // preserved for identity-only fragments.
  const dtype = val(specs?.detected_type);
  const isComponent =
    specs?.category === "Part" && Boolean(dtype) && Boolean(parentId) &&
    !parentId.toLowerCase().includes((dtype ?? "").toLowerCase());
  const label = isComponent
    ? `${formatType(dtype!)} — ${parentId}`
    : parentId || val(specs?.description) || "";

  // Phase 1 — minimal editable quantity field (gated by data presence, not a
  // frontend flag). The backend only populates `quantity` under INTAKE_TYPE_AWARE,
  // so flag-off runs never carry it and this control never renders (demo-unaffected).
  // Renders only for an identified/ready item so it doesn't clutter the "need more"
  // state. On commit, PUT the full specs back with the updated quantity.
  const [qty, setQty] = useState<number | null>(null);
  const [qtyBusy, setQtyBusy] = useState(false);
  useEffect(() => { setQty(specs?.quantity ?? null); }, [specs?.quantity]);
  const commitQty = async (next: number | null) => {
    if (!specs || next == null || next < 1 || qtyBusy) return;
    setQtyBusy(true);
    try {
      await seedAssetSpecs(runId, { ...specs, quantity: next });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
    } finally {
      setQtyBusy(false);
    }
  };
  // Stepper: clamp at 1, optimistic display, persist via commitQty (the PUT path).
  const stepQty = (delta: number) => {
    if (qtyBusy) return;
    const current = qty ?? specs?.quantity ?? 1;
    const next = Math.max(1, current + delta);
    if (next === current) return;
    setQty(next);
    void commitQty(next);
  };

  // Report this item's ready state up whenever it changes (onReady is stable via useCallback).
  useEffect(() => { onReady?.(runId, ready); }, [runId, ready, onReady]);

  // Editable-before-confirm (brief §2.2): the identification card's identity fields
  // (manufacturer / model / part number) can be corrected before Confirm, persisted
  // through the SAME PUT the quantity control already uses (seedAssetSpecs). The
  // sufficiency gate re-derives from the saved specs — clearing a field honestly
  // drops the card back to "need a little more".
  const [editing, setEditing] = useState(false);
  const [editBusy, setEditBusy] = useState(false);
  const [editVals, setEditVals] = useState({ manufacturer: "", model: "", part_number: "" });
  const openEdit = () => {
    setEditVals({
      manufacturer: val(specs?.manufacturer) ?? "",
      model: val(specs?.model) ?? "",
      part_number: val(specs?.part_number) ?? "",
    });
    setEditing(true);
  };
  const saveEdit = async () => {
    if (!specs || editBusy) return;
    setEditBusy(true);
    try {
      await seedAssetSpecs(runId, { ...specs, ...editVals });
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      setEditing(false);
    } catch {
      fire("Couldn't save those details — please try again.");
    } finally {
      setEditBusy(false);
    }
  };

  // The latest reply for THIS item: seeded from intake, then owned locally after each clarification.
  const [reply, setReply] = useState(initialReply);
  const [moreText, setMoreText] = useState("");
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // T5b — the family-variant block is parent state set on the 422; nothing re-
  // evaluated it once a chat answer refilled the specs, so a SATISFIED block
  // lingered on the card (the user answered, the backend cleared
  // _variant_disambig_pending and filled the variant attrs, but the card kept
  // rendering the stale question). Derive visibility from the LIVE specs: once
  // the user has ENGAGED with the ask (sent a chat answer — the same event that
  // clears the backend's _variant_disambig_pending), the block is satisfied when
  // every attr the 422 named is now answered in the refetched specs. Before
  // engagement the block stays, so the anti-hallucination case (attrs filled by
  // extraction but the user never confirmed the rating — still 422) renders
  // until they engage. The `_variant_disambig_pending` flag itself is stripped
  // from frontend asset_specs (api_server._orm_to_detail drops `_`-prefixed
  // keys), so satisfaction is derived from the real spec fields the missing
  // attrs map to (VARIANT_ATTR_FIELDS above), not the flag.
  const [engagedAfterBlock, setEngagedAfterBlock] = useState(false);
  const blockRef = useRef(variantBlock);
  useEffect(() => {
    if (variantBlock !== blockRef.current) {
      blockRef.current = variantBlock;
      setEngagedAfterBlock(false);   // a fresh 422 block -> not yet engaged
    }
  }, [variantBlock]);
  const attrAnswered = (attr: string): boolean => {
    const fields = VARIANT_ATTR_FIELDS[attr];
    if (!fields) return false;
    const s = specs as Record<string, unknown> | undefined;
    return fields.some((f) => {
      const v = s?.[f];
      return v != null && !BACKEND_NULL.has(String(v));
    });
  };
  const blockSatisfied =
    Boolean(variantBlock) &&
    engagedAfterBlock &&
    (variantBlock?.missing_attrs.length ?? 0) > 0 &&
    Boolean(variantBlock?.missing_attrs.every(attrAnswered));
  // Whether the variant block should RENDER. Replaces bare `variantBlock`
  // truthy-gates below so a satisfied block self-heals instead of lingering.
  const blockVisible = Boolean(variantBlock) && !blockSatisfied;

  const submit = async () => {
    const more = moreText.trim();
    if (!more || pending) return;
    setPending(true);
    setErr(null);
    try {
      const r = await sendMessage(runId, { content: more });
      setReply(r.message.content);
      setMoreText("");
      qc.invalidateQueries({ queryKey: queryKeys.runs.detail(runId) });
      qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
      // T5b: a chat answer sent from this card is the engagement event that
      // clears the backend's _variant_disambig_pending — record it so the
      // variant block is re-evaluated against the refetched specs (the run
      // query invalidation above refetches, then blockSatisfied derives hide).
      setEngagedAfterBlock(true);
    } catch (e) {
      // Real reason on THIS card: server detail (HTTP error) or null -> the connectivity line.
      setErr(apiErrorMessage(e) ?? "Couldn't reach the backend — is it running?");
    } finally {
      setPending(false);   // re-enable on response OR error so the user can retry
    }
  };

  // Honest escape (T5): "I don't know the rating — source the family as-is." Re-calls
  // confirm-intake with open_family=true so the run commits as an honest open-family /
  // spec-based source rather than a silent variant pick. Single-item proceed: the other
  // basket items already advanced to sourcing in the same advance() attempt, so only this
  // blocked run remains. On success navigate as the normal advance does; a failure (e.g.
  // 409) hits the existing toast — no special casing.
  const [escaping, setEscaping] = useState(false);
  const escapeFamily = async () => {
    if (escaping) return;
    setEscaping(true);
    try {
      await confirmIntake(runId, false, true);
      router.push(`/parts/${runId}`);
    } catch {
      fire("Couldn't start sourcing — please try again.");
    } finally {
      setEscaping(false);
    }
  };

  return (
    <div className="proc-id" style={{ marginTop: 10 }}>
      <div className="id-top">
        <span className="id-ic"><ProcIcon name={ready ? "toolbox" : "alert"} size={20} color={ready ? undefined : "var(--st-overdue)"} /></span>
        <div style={{ flex: 1 }}>
          <div className="id-kick">{ready ? "Part identified" : "Need a little more"}</div>
          <div className="id-name">{label || (multiPart ? "Multiple parts detected" : "Unidentified part")}</div>
          {ready && pn && <div className="id-meta">Part no. <b>{pn}</b>{mfg ? <> · {mfg}</> : null}</div>}
          {ready && !pn && specs?.spec_based_sourcing && (
            <div className="id-meta">Matching by category — no exact part number needed.</div>
          )}
          {/* Real extraction confidence (0–100 from the backend) — shown, never invented. */}
          {ready && (specs?.part_id_confidence ?? specs?.manufacturer_confidence) != null && (
            <div className="id-meta" style={{ marginTop: 2 }}>
              Identified from your request · confidence{" "}
              {Math.round(specs?.part_id_confidence ?? specs!.manufacturer_confidence)}%
            </div>
          )}
          {ready && !editing && (
            <button
              className="proc-btn"
              data-kind="quiet"
              style={{ marginTop: 8, padding: "4px 8px", fontSize: 12 }}
              onClick={openEdit}
            >
              Edit details
            </button>
          )}
          {editing && (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10, maxWidth: 420 }}>
              {([["manufacturer", "Manufacturer"], ["model", "Model"], ["part_number", "Part number"]] as const).map(([k, l]) => (
                <label key={k} style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12, fontWeight: 600, color: "var(--muted)" }}>
                  {l}
                  <input
                    className="proc-idinput"
                    value={editVals[k]}
                    disabled={editBusy}
                    onChange={(e) => setEditVals((v) => ({ ...v, [k]: e.target.value }))}
                  />
                </label>
              ))}
              <div style={{ display: "flex", gap: 8 }}>
                <button className="proc-btn" data-kind="quiet" disabled={editBusy} onClick={() => setEditing(false)}>Cancel</button>
                <button className="proc-btn" data-kind="primary" disabled={editBusy} onClick={saveEdit}>
                  {editBusy ? "Saving…" : "Save details"}
                </button>
              </div>
            </div>
          )}
          {ready && specs?.quantity != null && (
            <div className="proc-qty" style={{ marginTop: 10 }}>
              <span className="ql">Qty</span>
              <div className="proc-stepper">
                <button
                  type="button"
                  className="proc-stepper-btn"
                  aria-label="Decrease quantity"
                  disabled={qtyBusy || (qty ?? specs.quantity) <= 1}
                  onClick={() => stepQty(-1)}
                >−</button>
                <div className="proc-stepper-val" aria-live="polite">{qty ?? specs.quantity}</div>
                <button
                  type="button"
                  className="proc-stepper-btn"
                  aria-label="Increase quantity"
                  disabled={qtyBusy}
                  onClick={() => stepQty(1)}
                >+</button>
              </div>
            </div>
          )}
          {!ready && reply && <div className="id-meta" style={{ marginTop: 4 }}>{reply}</div>}
          {/* Family-variant 422 (T5): the run IS ready by the sufficiency gate (mfg + model,
              no PN) but confirm_intake's binding guard blocked it. Re-surface the ask here —
              pending frames "confirm the rating you nearly named" vs "provide the rating" —
              reusing the id-kick/id-meta question styling. The input below (re-shown via the
              !ready || variantBlock gate) is the primary path: it submits through the EXISTING
              /messages flow so the answer persists into asset_specs before the next confirm
              (the guard reads persisted specs — a local-only answer wouldn't clear it).
              Gated on blockVisible (T5b) so a satisfied block stops rendering
              once the refetched specs answer every named variant attr. */}
          {blockVisible && variantBlock && (
            <>
              <div className="id-kick" style={{ marginTop: 6 }}>
                {variantBlock.pending
                  ? `Confirm the rating for your ${variantBlock.model}`
                  : "Provide the rating"}
              </div>
              <div className="id-meta" style={{ marginTop: 2 }}>{variantBlock.message}</div>
            </>
          )}
        </div>
        {onRemove && (
          <button
            className="proc-btn"
            data-kind="quiet"
            style={{ padding: "2px 8px", alignSelf: "flex-start" }}
            title="Remove this part"
            onClick={() => onRemove(runId)}
          >
            Remove
          </button>
        )}
      </div>

      {(!ready || blockVisible) && (
        <div className="id-actions" style={{ marginTop: 10 }}>
          <input
            className="proc-idinput"
            value={moreText}
            onChange={(e) => setMoreText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            placeholder="Add the manufacturer, model, or what it does…"
            disabled={pending}
          />
          <button className="proc-btn" data-kind="primary" disabled={pending || !moreText.trim()} onClick={submit}>
            {pending ? "Sending…" : "Send"}
          </button>
        </div>
      )}

      {/* Honest escape (T5): a link-style secondary action, NOT a primary button — the
          primary path is answering the rating in chat. Re-calls confirm-intake with
          open_family=true and proceeds; failure falls back to the existing toast. */}
      {blockVisible && (
        <button
          className="proc-btn"
          data-kind="quiet"
          style={{ marginTop: 8, padding: "2px 0", textDecoration: "underline" }}
          disabled={escaping}
          onClick={escapeFamily}
        >
          {escaping ? "Sourcing the family as-is…" : "I don't know the rating — source the family as-is"}
        </button>
      )}

      {err && (
        <div className="id-meta" style={{ color: "var(--st-overdue)", marginTop: 8 }}>{err}</div>
      )}
    </div>
  );
}
