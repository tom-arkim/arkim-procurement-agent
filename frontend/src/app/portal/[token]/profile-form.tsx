"use client";

/**
 * ProfileForm — the low-friction single-pass edit of the trio
 * (brands / classes / ship-area) on ONE page (anti-Ariba: no multi-screen wizard).
 *
 * The tri-state brand relationship is the centerpiece: only the supplier
 * authoritatively knows authorized vs carries vs aftermarket-compatible, so it
 * gets the most prominent, clearest control — and the supplier can ADD their
 * own brands here (not wait for a rep). One "Submit for review" action proposes
 * a revision (NEVER writes the registry — the concierge approves).
 *
 * Input is owned by the parent (ClaimPage) and passed back via onChange, so a
 * failed submit preserves every edit (the supplier never re-enters).
 */

import { useId, useState } from "react";
import { BRAND_NAME } from "@/lib/brand";
import type { BrandRelationship, ShipArea } from "@/lib/portal-api";
import type { ProposeRevisionBody } from "@/lib/portal-api";

export interface BrandRow {
  brand_id: string;
  relationship: BrandRelationship;
}

export interface ClassRow {
  class_id: string;
  is_core: boolean;
}

export interface FormState {
  brands: BrandRow[];
  classes: ClassRow[];
  shipArea: ShipArea;
}

const RELATIONSHIPS: { value: BrandRelationship; label: string; help: string }[] = [
  {
    value: "AUTHORIZED",
    label: "Authorized distributor",
    help: "You're an authorized distributor for this brand.",
  },
  {
    value: "CARRIES",
    label: "Carries / stocks",
    help: "You stock and sell this brand but aren't an authorized distributor.",
  },
  {
    value: "AFTERMARKET_COMPATIBLE",
    label: "Aftermarket-compatible",
    help: "You supply aftermarket-compatible parts for this brand (not the OEM).",
  },
];

// The default relationship for a newly added brand. "Carries / stocks" is the
// most common and the lowest-friction default — the supplier picks the stronger
// AUTHORIZED or the aftermarket variant explicitly when it applies.
const DEFAULT_NEW_BRAND_REL: BrandRelationship = "CARRIES";

const US_STATES = [
  "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
  "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
  "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
  "VA","WA","WV","WI","WY",
];

export function ProfileForm({
  initial,
  aftermarketDisclosure,
  onChange,
  onSubmit,
  submitting,
}: {
  initial: FormState;
  aftermarketDisclosure: string | null;
  onChange: (next: FormState) => void;
  onSubmit: (next: FormState) => void;
  submitting: boolean;
}) {
  const formId = useId();
  // Local state for the add-brand control. The brand list itself is owned by
  // the parent (ClaimPage) via `initial` + onChange, so it survives a failed
  // submit. Only the in-progress add input lives here.
  const [newBrand, setNewBrand] = useState("");
  const [newRel, setNewRel] = useState<BrandRelationship>(DEFAULT_NEW_BRAND_REL);
  const [addError, setAddError] = useState<string | null>(null);

  const setBrandRel = (i: number, rel: BrandRelationship) => {
    const brands = initial.brands.map((b, idx) =>
      idx === i ? { ...b, relationship: rel } : b,
    );
    onChange({ ...initial, brands });
  };

  const removeBrand = (i: number) => {
    // The propose-revision payload is a full brand snapshot, so dropping a row
    // here proposes removing it (the concierge approves — nothing is live yet).
    const brands = initial.brands.filter((_, idx) => idx !== i);
    onChange({ ...initial, brands });
  };

  const addBrand = () => {
    const id = newBrand.trim();
    if (!id) {
      setAddError("Enter a brand name first.");
      return;
    }
    // De-duplicate case-insensitively so the supplier can't add the same brand
    // twice (a typo variant like "Goulds" / "goulds" would double-count).
    const exists = initial.brands.some(
      (b) => b.brand_id.toLowerCase() === id.toLowerCase(),
    );
    if (exists) {
      setAddError(`"${id}" is already in your list — set its relationship above.`);
      return;
    }
    onChange({
      ...initial,
      brands: [...initial.brands, { brand_id: id, relationship: newRel }],
    });
    setNewBrand("");
    setAddError(null);
    // Keep `newRel` as-is so adding several brands of the same relationship is
    // one click each (low-friction, single-pass).
  };

  const toggleCore = (i: number) => {
    const classes = initial.classes.map((c, idx) =>
      idx === i ? { ...c, is_core: !c.is_core } : c,
    );
    onChange({ ...initial, classes });
  };

  const setShipArea = (sa: ShipArea) => {
    onChange({ ...initial, shipArea: sa });
  };

  const toggleState = (code: string) => {
    const cur =
      initial.shipArea && initial.shipArea.kind === "STATES"
        ? initial.shipArea.states
        : [];
    const has = cur.includes(code);
    const states = has ? cur.filter((s) => s !== code) : [...cur, code].sort();
    setShipArea({ kind: "STATES", states });
  };

  return (
    <form
      className="portal-form"
      id={formId}
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(initial);
      }}
    >
      {/* Section caption */}
      <p className="portal-form-eyebrow">Confirm your profile</p>
      <h2 className="portal-form-heading">
        Tell {BRAND_NAME} what you supply
      </h2>
      <p className="portal-form-sub">
        One quick pass — brands, classes, and where you ship. Your rep reviews
        changes before they go live.
      </p>

      {/* BRANDS — the centerpiece (tri-state relationship, supplier-editable) */}
      <fieldset className="portal-fieldset">
        <legend className="portal-legend portal-legend--centerpiece">
          Brands
          <span className="portal-legend-hint">your relationship to each — this is what buyers care most about</span>
        </legend>
        {initial.brands.length === 0 && (
          <p className="portal-empty">
            No brands listed yet — add the brands you carry below.
          </p>
        )}
        <ul className="portal-brand-list">
          {initial.brands.map((b, i) => (
            <li key={b.brand_id} className="portal-brand-row">
              <div className="portal-brand-head">
                <span className="portal-brand-name">{b.brand_id}</span>
                <button
                  type="button"
                  className="portal-brand-remove"
                  aria-label={`Remove ${b.brand_id}`}
                  onClick={() => removeBrand(i)}
                >
                  Remove
                </button>
              </div>
              <div className="portal-rel-group" role="radiogroup" aria-label={`Relationship to ${b.brand_id}`}>
                {RELATIONSHIPS.map((r) => {
                  const checked = b.relationship === r.value;
                  return (
                    <label
                      key={r.value}
                      className={
                        "portal-rel-option" + (checked ? " portal-rel-option--checked" : "")
                      }
                      title={r.help}
                    >
                      <input
                        type="radio"
                        name={`${formId}-rel-${i}`}
                        value={r.value}
                        checked={checked}
                        onChange={() => setBrandRel(i, r.value)}
                      />
                      <span>{r.label}</span>
                    </label>
                  );
                })}
              </div>
            </li>
          ))}
        </ul>

        {/* Add-brand control — the supplier adds their own brands here (the
            centerpiece: only they authoritatively know authorized vs carries vs
            aftermarket-compatible). Type the brand, pick its relationship, Add. */}
        <div className="portal-brand-add">
          <label className="portal-brand-add-name">
            <span className="portal-brand-add-label">Brand name</span>
            <input
              type="text"
              className="portal-brand-add-input"
              value={newBrand}
              onChange={(e) => {
                setNewBrand(e.target.value);
                if (addError) setAddError(null);
              }}
              placeholder="e.g. Goulds, ITT, Grundfos"
              aria-label="Brand name to add"
            />
          </label>
          <div
            className="portal-rel-group portal-rel-group--add"
            role="radiogroup"
            aria-label="Relationship for the new brand"
          >
            {RELATIONSHIPS.map((r) => {
              const checked = newRel === r.value;
              return (
                <label
                  key={r.value}
                  className={
                    "portal-rel-option" + (checked ? " portal-rel-option--checked" : "")
                  }
                  title={r.help}
                >
                  <input
                    type="radio"
                    name={`${formId}-new-rel`}
                    value={r.value}
                    checked={checked}
                    onChange={() => setNewRel(r.value)}
                  />
                  <span>{r.label}</span>
                </label>
              );
            })}
          </div>
          {addError && (
            <p className="portal-brand-add-error" role="alert">{addError}</p>
          )}
          <button
            type="button"
            className="portal-brand-add-btn"
            onClick={addBrand}
          >
            + Add brand
          </button>
        </div>
      </fieldset>

      {/* CLASSES */}
      <fieldset className="portal-fieldset">
        <legend className="portal-legend">
          Classes
          <span className="portal-legend-hint">mark the categories that are core to what you do</span>
        </legend>
        {initial.classes.length === 0 && (
          <p className="portal-empty">
            No classes listed yet. Your rep can add classes when they review your profile.
          </p>
        )}
        <ul className="portal-class-list">
          {initial.classes.map((c, i) => (
            <li key={c.class_id} className="portal-class-row">
              <label className="portal-core-toggle">
                <input
                  type="checkbox"
                  checked={c.is_core}
                  onChange={() => toggleCore(i)}
                />
                <span className="portal-class-name">{c.class_id}</span>
                {c.is_core && <span className="portal-core-tag">core</span>}
              </label>
            </li>
          ))}
        </ul>
      </fieldset>

      {/* SHIP AREA */}
      <fieldset className="portal-fieldset">
        <legend className="portal-legend">
          Ship area
          <span className="portal-legend-hint">where you can deliver</span>
        </legend>
        <div className="portal-ship-group" role="radiogroup" aria-label="Ship area">
          <label
            className={
              "portal-ship-option" +
              (initial.shipArea?.kind === "NATIONWIDE_US" ? " portal-ship-option--checked" : "")
            }
          >
            <input
              type="radio"
              name={`${formId}-ship`}
              checked={initial.shipArea?.kind === "NATIONWIDE_US"}
              onChange={() => setShipArea({ kind: "NATIONWIDE_US" })}
            />
            <span>Nationwide US</span>
          </label>
          <label
            className={
              "portal-ship-option" +
              (initial.shipArea?.kind === "STATES" ? " portal-ship-option--checked" : "")
            }
          >
            <input
              type="radio"
              name={`${formId}-ship`}
              checked={initial.shipArea?.kind === "STATES"}
              onChange={() =>
                setShipArea({
                  kind: "STATES",
                  states:
                    initial.shipArea?.kind === "STATES"
                      ? initial.shipArea.states
                      : [],
                })
              }
            />
            <span>Specific states</span>
          </label>
        </div>
        {initial.shipArea?.kind === "STATES" && (
          <div className="portal-states-grid" role="group" aria-label="Select states you ship to">
            {US_STATES.map((code) => {
              const on = initial.shipArea?.kind === "STATES" && initial.shipArea.states.includes(code);
              return (
                <label
                  key={code}
                  className={"portal-state-chip" + (on ? " portal-state-chip--on" : "")}
                >
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => toggleState(code)}
                  />
                  <span>{code}</span>
                </label>
              );
            })}
          </div>
        )}
      </fieldset>

      {/* Aftermarket disclosure — shown when the supplier carries aftermarket brands,
          so they see what buyers see. */}
      {aftermarketDisclosure && (
        <aside className="portal-disclosure" role="note">
          <p className="portal-disclosure-eyebrow">Aftermarket parts notice</p>
          <p className="portal-disclosure-body">{aftermarketDisclosure}</p>
        </aside>
      )}

      {/* Submit — the single action. "Submit for review", never "Save". */}
      <div className="portal-form-actions">
        <button
          type="submit"
          className="portal-submit"
          disabled={submitting}
        >
          {submitting ? "Submitting…" : "Submit for review"}
        </button>
        <p className="portal-submit-note">
          Nothing goes live until your {BRAND_NAME} representative approves your changes.
        </p>
      </div>
    </form>
  );
}

// Re-export for type-only consumers.
export type { ProposeRevisionBody };
