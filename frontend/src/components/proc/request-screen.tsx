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
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { createRun, sendMessage, confirmIntake, uploadNameplate, seedAssetSpecs } from "@/lib/api";
import { useRun } from "@/lib/queries";
import { queryKeys, apiErrorMessage } from "@/lib/query-client";
import { ProcIcon } from "./proc-icon";
import { ProcHead, ArkimLoader } from "./proc-ui";
import { useProcToast } from "./proc-shell";
import type { AssetSpecs } from "@/types";

const QUICK = [
  "Filter for the main compressor",
  "V-belt for the transfer belt",
  "Door seal for the tray washer",
];

const NULLS = new Set(["", "n/a", "null", "none", "unknown-pn", "unknown", "tbd"]);
const val = (v?: string | null) => (v && !NULLS.has(String(v).toLowerCase().trim()) ? String(v) : undefined);

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

/** One part in the request, each backed by its OWN run (the per-item / basket model). The
 *  parent holds the runId + the latest intake reply; specs / partLabel / ready-state are
 *  derived per-card from useRun(runId) (Stage A data layer). */
type IntakeItem = { runId: string; reply: string; multiPart?: boolean };

export function RequestScreen() {
  const router = useRouter();
  const qc = useQueryClient();
  const fire = useProcToast();

  const [stage, setStage] = useState<Stage>("entry");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
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
    try {
      await Promise.all(items.map((it) => confirmIntake(it.runId, false)));
      router.push(`/parts/${items[0].runId}`);
    } catch {
      fire("Couldn't start sourcing — please try again.");
      setAdvancing(false);   // stay on the page so the user can retry
    }
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
          <div className="proc-ask">
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
            No part number? That&apos;s fine — describe what it does and we&apos;ll figure it out.
          </div>
        </>
      )}

      {/* WORKING */}
      {stage === "working" && (
        <div className="proc-loading">
          <ArkimLoader size={52} />
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
              onReady={handleReady}
              // Removable only when there's more than one card — you can't remove the last part.
              onRemove={items.length > 1 ? removeItem : undefined}
            />
          ))}

          {/* + add another part — a new run created into the SAME basket group. The button and
              Enter share one path (handleAddClick / addPart); an empty box focuses, never no-ops. */}
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
  onReady,
  onRemove,
}: {
  runId: string;
  initialReply: string;
  multiPart?: boolean;
  onReady?: (runId: string, ready: boolean) => void;
  onRemove?: (runId: string) => void;
}) {
  const qc = useQueryClient();
  const { data: run } = useRun(runId, { enabled: Boolean(runId) });
  const specs = run?.asset_specs;
  const ready = specsReady(specs);
  const pn = val(specs?.part_number);
  const mfg = val(specs?.manufacturer);
  const label =
    [mfg, val(specs?.model) || pn].filter(Boolean).join(" ") || val(specs?.description) || "";

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

  // Report this item's ready state up whenever it changes (onReady is stable via useCallback).
  useEffect(() => { onReady?.(runId, ready); }, [runId, ready, onReady]);

  // The latest reply for THIS item: seeded from intake, then owned locally after each clarification.
  const [reply, setReply] = useState(initialReply);
  const [moreText, setMoreText] = useState("");
  const [pending, setPending] = useState(false);
  const [err, setErr] = useState<string | null>(null);

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
    } catch (e) {
      // Real reason on THIS card: server detail (HTTP error) or null -> the connectivity line.
      setErr(apiErrorMessage(e) ?? "Couldn't reach the backend — is it running?");
    } finally {
      setPending(false);   // re-enable on response OR error so the user can retry
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
          {ready && specs?.quantity != null && (
            <div className="id-meta" style={{ marginTop: 4, display: "flex", alignItems: "center", gap: 6 }}>
              Qty:
              <input
                type="number"
                min={1}
                value={qty ?? specs.quantity}
                disabled={qtyBusy}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  setQty(Number.isFinite(n) ? n : null);
                }}
                onBlur={() => {
                  const n = qty ?? specs.quantity ?? 1;
                  const next = Math.max(1, Math.floor(n));
                  if (next !== (specs.quantity ?? null)) void commitQty(next);
                  else setQty(specs.quantity ?? null);
                }}
                style={{ width: 64, padding: "2px 6px" }}
                aria-label="Quantity"
              />
            </div>
          )}
          {!ready && reply && <div className="id-meta" style={{ marginTop: 4 }}>{reply}</div>}
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

      {!ready && (
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

      {err && (
        <div className="id-meta" style={{ color: "var(--st-overdue)", marginTop: 8 }}>{err}</div>
      )}
    </div>
  );
}
