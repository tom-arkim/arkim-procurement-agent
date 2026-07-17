"use client";

/**
 * QuoteForm — the five required fields + two optional (spec §4), one screen,
 * mobile-friendly, ≥44px touch targets, portal-surface palette.
 *
 * Required: quote/reference number, unit price (USD), quantity quoted
 * (prefilled from the RFQ, editable — partial quotes are real), lead time
 * (days or "in stock"), part-number confirmation (prefilled with the requested
 * PN; an EDIT is flagged inline as "quoting an alternative" — the wrong-part
 * gate runs server-side; the note here is honesty, not enforcement).
 * Optional: freight (amount or "included"), valid-until (defaults to the RFQ
 * window server-side), notes (to the concierge, not shown raw to the buyer).
 *
 * Client validation is minimal-and-honest (required presence + positive
 * numbers); the server is authoritative.
 */

import { useMemo, useState } from "react";

export interface QuoteFormState {
  quoteNumber: string;
  unitPrice: string;
  quantity: string;
  leadTimeDays: string;
  inStock: boolean;
  partNumber: string;
  freight: string;
  validUntil: string;
  notes: string;
}

function normalizePn(pn: string): string {
  return pn.replace(/[^a-zA-Z0-9]/g, "").toUpperCase();
}

export function QuoteForm({
  initial,
  requestedPartNumber,
  onChange,
  onSubmit,
  submitting,
}: {
  initial: QuoteFormState;
  requestedPartNumber: string | null;
  onChange: (next: QuoteFormState) => void;
  onSubmit: (next: QuoteFormState) => void;
  submitting: boolean;
}) {
  const [form, setFormLocal] = useState<QuoteFormState>(initial);
  const [touchedSubmit, setTouchedSubmit] = useState(false);

  const set = (patch: Partial<QuoteFormState>) => {
    const next = { ...form, ...patch };
    setFormLocal(next);
    onChange(next);
  };

  const pnEdited = useMemo(() => {
    if (!requestedPartNumber || !form.partNumber.trim()) return false;
    return normalizePn(form.partNumber) !== normalizePn(requestedPartNumber);
  }, [form.partNumber, requestedPartNumber]);

  const errors = useMemo(() => {
    const e: string[] = [];
    if (!form.quoteNumber.trim()) e.push("quote number");
    if (!(Number(form.unitPrice) > 0)) e.push("unit price");
    if (!(Number(form.quantity) > 0)) e.push("quantity");
    if (!form.inStock && !(Number(form.leadTimeDays) > 0)) e.push("lead time");
    return e;
  }, [form]);

  const handleSubmit = (ev: React.FormEvent) => {
    ev.preventDefault();
    setTouchedSubmit(true);
    if (errors.length === 0) onSubmit(form);
  };

  return (
    <form className="quote-form" onSubmit={handleSubmit} noValidate>
      <div className="quote-field">
        <label className="quote-label" htmlFor="q-number">
          Your quote / reference number
        </label>
        <input
          id="q-number"
          className="quote-input"
          type="text"
          value={form.quoteNumber}
          onChange={(e) => set({ quoteNumber: e.target.value })}
          placeholder="e.g. Q-10412"
          required
        />
      </div>

      <div className="quote-field-row">
        <div className="quote-field">
          <label className="quote-label" htmlFor="q-price">
            Unit price (USD)
          </label>
          <input
            id="q-price"
            className="quote-input"
            type="number"
            inputMode="decimal"
            min="0.01"
            step="0.01"
            value={form.unitPrice}
            onChange={(e) => set({ unitPrice: e.target.value })}
            placeholder="0.00"
            required
          />
        </div>
        <div className="quote-field">
          <label className="quote-label" htmlFor="q-qty">
            Quantity quoted
          </label>
          <input
            id="q-qty"
            className="quote-input"
            type="number"
            inputMode="numeric"
            min="1"
            step="1"
            value={form.quantity}
            onChange={(e) => set({ quantity: e.target.value })}
            required
          />
          <p className="quote-hint">
            Partial quantities are fine — quote what you can supply.
          </p>
        </div>
      </div>

      <div className="quote-field">
        <span className="quote-label">Lead time</span>
        <div className="quote-lead-row">
          <label className="quote-instock" htmlFor="q-instock">
            <input
              id="q-instock"
              type="checkbox"
              checked={form.inStock}
              onChange={(e) => set({ inStock: e.target.checked })}
            />
            In stock now
          </label>
          <div className="quote-lead-days">
            <input
              id="q-lead"
              className="quote-input"
              type="number"
              inputMode="numeric"
              min="1"
              step="1"
              value={form.leadTimeDays}
              onChange={(e) => set({ leadTimeDays: e.target.value })}
              disabled={form.inStock}
              placeholder="days"
              aria-label="Lead time in days"
            />
            <span className="quote-lead-suffix">days</span>
          </div>
        </div>
      </div>

      <div className="quote-field">
        <label className="quote-label" htmlFor="q-pn">
          Part number you are quoting
        </label>
        <input
          id="q-pn"
          className="quote-input"
          type="text"
          value={form.partNumber}
          onChange={(e) => set({ partNumber: e.target.value })}
        />
        {pnEdited ? (
          <p className="quote-pn-note" role="status">
            This differs from the requested part number
            {requestedPartNumber ? ` (${requestedPartNumber})` : ""} — we&apos;ll
            treat it as an equivalent alternative and confirm it before it goes
            to the buyer.
          </p>
        ) : (
          <p className="quote-hint">
            Pre-filled with the requested part number — edit it only if you are
            quoting an alternative.
          </p>
        )}
      </div>

      <details className="quote-optional">
        <summary className="quote-optional-summary">
          Optional: freight, validity, notes
        </summary>
        <div className="quote-optional-body">
          <div className="quote-field">
            <label className="quote-label" htmlFor="q-freight">
              Freight / shipping
            </label>
            <input
              id="q-freight"
              className="quote-input"
              type="text"
              value={form.freight}
              onChange={(e) => set({ freight: e.target.value })}
              placeholder='e.g. "$25" or "included"'
            />
          </div>
          <div className="quote-field">
            <label className="quote-label" htmlFor="q-valid">
              Quote valid until
            </label>
            <input
              id="q-valid"
              className="quote-input"
              type="date"
              value={form.validUntil}
              onChange={(e) => set({ validUntil: e.target.value })}
            />
            <p className="quote-hint">
              Leave blank to keep it open for the request window.
            </p>
          </div>
          <div className="quote-field">
            <label className="quote-label" htmlFor="q-notes">
              Notes
            </label>
            <textarea
              id="q-notes"
              className="quote-input quote-textarea"
              value={form.notes}
              onChange={(e) => set({ notes: e.target.value })}
              rows={3}
              placeholder="Anything we should know (goes to our team, not straight to the buyer)"
            />
          </div>
        </div>
      </details>

      {touchedSubmit && errors.length > 0 && (
        <p className="portal-brand-add-error" role="alert">
          Please fill in: {errors.join(", ")}.
        </p>
      )}

      <div className="portal-form-actions">
        <button className="portal-submit" type="submit" disabled={submitting}>
          {submitting ? "Submitting…" : "Submit quote"}
        </button>
        <p className="portal-submit-note">
          No account needed. Submitting again later replaces this quote.
        </p>
      </div>
    </form>
  );
}
