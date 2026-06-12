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

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { createRun, sendMessage, confirmIntake, uploadNameplate } from "@/lib/api";
import { useRun } from "@/lib/queries";
import { queryKeys } from "@/lib/query-client";
import { ProcIcon } from "./proc-icon";
import { ProcHead, ChevLoader } from "./proc-ui";
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
  const mfg = val(specs.manufacturer);
  return Boolean(mfg && (val(specs.part_number) || val(specs.model) || specs.spec_based_sourcing));
}

type Stage = "entry" | "working" | "identify" | "error";

export function RequestScreen() {
  const router = useRouter();
  const qc = useQueryClient();
  const fire = useProcToast();

  const [stage, setStage] = useState<Stage>("entry");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [reply, setReply] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [moreInput, setMoreInput] = useState(false);
  const [moreText, setMoreText] = useState("");

  const { data: run } = useRun(runId ?? "", { enabled: Boolean(runId) });

  const refresh = (id: string) => {
    qc.invalidateQueries({ queryKey: queryKeys.runs.detail(id) });
    qc.invalidateQueries({ queryKey: queryKeys.runs.all() });
  };

  const start = async () => {
    const desc = text.trim();
    if (!desc && !file) return;
    setStage("working");
    try {
      const created = await createRun({});
      setRunId(created.id);
      if (file) {
        // Nameplate photo -> vision extraction updates the run's asset_specs.
        const up = await uploadNameplate(created.id, file);
        setReply(up.message?.content ?? "Read the nameplate from your photo.");
      } else {
        const r = await sendMessage(created.id, { content: desc });
        setReply(r.message.content);
      }
      refresh(created.id);
      setStage("identify");
    } catch {
      setStage("error");
    }
  };

  const sendMore = async () => {
    const more = moreText.trim();
    if (!more || !runId) return;
    setBusy(true);
    try {
      const r = await sendMessage(runId, { content: more });
      setReply(r.message.content);
      refresh(runId);
      setMoreInput(false);
      setMoreText("");
    } catch {
      fire("Message failed — please try again.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (exactOnly: boolean) => {
    if (!runId) return;
    setBusy(true);
    try {
      await confirmIntake(runId, exactOnly);
      router.push(`/parts/${runId}`);
    } catch {
      fire("Couldn't start sourcing — try adding a bit more detail.");
      setBusy(false);
    }
  };

  const specs = run?.asset_specs;
  const ready = specsReady(specs);
  // Exact part number on file -> offer the exact-only choice (the honesty branch).
  // Spec-based (no PN) -> equivalents are the whole point, so don't offer it.
  const hasPartNumber = Boolean(val(specs?.part_number)) && !specs?.spec_based_sourcing;
  const partName =
    [val(specs?.manufacturer), val(specs?.model) || val(specs?.part_number)].filter(Boolean).join(" ") ||
    val(specs?.description) ||
    "Your part";

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
              onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start(); }}
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
        <div className="proc-working">
          <ChevLoader size={20} />
          <div>
            <div className="w-t">{file ? "Reading the nameplate…" : "Identifying part…"}</div>
            <div className="w-s">{file ? "Pulling the part details from your photo." : "Checking service records and manuals."}</div>
          </div>
        </div>
      )}

      {/* ERROR */}
      {stage === "error" && (
        <div className="proc-working">
          <ProcIcon name="alert" size={20} color="var(--st-overdue)" />
          <div>
            <div className="w-t">Couldn&apos;t process that request.</div>
            <div className="w-s">Is the backend running? <button className="proc-btn" data-kind="quiet" style={{ padding: "2px 6px" }} onClick={() => setStage("entry")}>Try again</button></div>
          </div>
        </div>
      )}

      {/* IDENTIFY */}
      {stage === "identify" && (
        <div style={{ marginTop: 8 }}>
          <div className="proc-kicker">{ready && !moreInput ? "We think this is…" : "A quick question"}</div>
          <div className="proc-id">
            <div className="id-top">
              <span className="id-ic"><ProcIcon name={ready ? "toolbox" : "alert"} size={22} /></span>
              <div style={{ flex: 1 }}>
                {ready && !moreInput ? (
                  <>
                    <div className="id-kick">Part identified</div>
                    <div className="id-name">{partName}</div>
                    <div className="id-meta">
                      {val(specs?.part_number) && <>Part no. <b>{val(specs?.part_number)}</b>{val(specs?.manufacturer) ? <> · {val(specs?.manufacturer)}</> : null}<br /></>}
                      {specs?.spec_based_sourcing && <>Matching by category — no exact part number needed.</>}
                    </div>
                    {reply && <div className="id-src">{reply}</div>}
                  </>
                ) : (
                  <>
                    <div className="id-kick">Need a little more</div>
                    <div className="id-meta" style={{ marginTop: 0 }}>{reply ?? "Tell me a bit more about the part."}</div>
                  </>
                )}
              </div>
            </div>

            {moreInput || !ready ? (
              <div className="id-actions">
                <input
                  className="proc-idinput"
                  value={moreText}
                  onChange={(e) => setMoreText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter") sendMore(); }}
                  placeholder="Add the manufacturer, model, or what it does…"
                  autoFocus
                />
                <button className="proc-btn" data-kind="primary" disabled={busy || !moreText.trim()} onClick={sendMore}>
                  {busy ? "Sending…" : "Send"}
                </button>
                {ready && (
                  <button className="proc-btn" data-kind="quiet" onClick={() => setMoreInput(false)} disabled={busy}>Cancel</button>
                )}
              </div>
            ) : hasPartNumber ? (
              <>
                <div className="id-reply">
                  We have the exact part number. Exact replacements are always a safe fit. Equivalents
                  can be cheaper, but we&apos;ll only suggest them when there&apos;s enough detail to check the fit.
                </div>
                <div className="id-actions">
                  <button className="proc-btn" data-kind="primary" disabled={busy} onClick={() => confirm(false)}>
                    <ProcIcon name="checkCircle" size={15} />{busy ? "Starting…" : "Find options (incl. equivalents)"}
                  </button>
                  <button className="proc-btn" disabled={busy} onClick={() => confirm(true)}>
                    Exact replacements only
                  </button>
                  <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => setMoreInput(true)}>
                    Not quite — add detail
                  </button>
                </div>
              </>
            ) : (
              <div className="id-actions">
                <button className="proc-btn" data-kind="primary" disabled={busy} onClick={() => confirm(false)}>
                  <ProcIcon name="checkCircle" size={15} />{busy ? "Starting…" : "Yes — find options"}
                </button>
                <button className="proc-btn" data-kind="quiet" disabled={busy} onClick={() => setMoreInput(true)}>
                  Not quite — add detail
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
