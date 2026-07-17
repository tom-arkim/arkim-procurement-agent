"use client";

/**
 * QuotePage — the interactive body of the public quote-submission page.
 *
 * State machine:
 *  loading    — branded spinner while the token context fetches.
 *  ready      — the request summary (hero) + the five-field form.
 *  closed     — honest "this request has closed" (dead RFQ; NOT an error).
 *  rejected   — uniform "link no longer valid" (invalid/flag-off/network).
 *  submitted  — confirmation + the claim pitch (path A, unclaimed only).
 *               An instantly-active quote confirms plainly; a review-flagged
 *               one says so honestly ("being double-checked") — never a fake
 *               "live" claim on a flagged submission.
 *  soft-error — submit failed; input PRESERVED, honest retry message.
 *
 * The token is passed as a prop, held in a ref for the page lifetime, never
 * persisted/logged. See page.tsx security notes.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { BRAND_NAME } from "@/lib/brand";
import {
  getQuoteContext,
  submitQuote,
  type QuoteContext,
  type QuoteSubmissionBody,
  type QuoteSubmitResponse,
} from "@/lib/quote-api";
import { QuoteForm, type QuoteFormState } from "./quote-form";
import { GoferLoader } from "@/components/ui/gofer-loader";

type Phase =
  | "loading"
  | "ready"
  | "closed"
  | "rejected"
  | "submitted"
  | "soft-error";

export function QuotePage({ token }: { token: string }) {
  const tokenRef = useRef(token);
  const [phase, setPhase] = useState<Phase>("loading");
  const [context, setContext] = useState<QuoteContext | null>(null);
  // Form state lives here so it survives the soft-error transition.
  const [form, setForm] = useState<QuoteFormState | null>(null);
  const [result, setResult] = useState<QuoteSubmitResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setPhase("loading");
    getQuoteContext(tokenRef.current).then((res) => {
      if (cancelled) return;
      if (res.ok) {
        if (res.data.state === "closed") {
          setPhase("closed");
          return;
        }
        setContext(res.data);
        setForm(contextToForm(res.data));
        setPhase("ready");
      } else if ("closed" in res && res.closed) {
        setPhase("closed");
      } else {
        setPhase("rejected");
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSubmit = useCallback(async (nextForm: QuoteFormState) => {
    setForm(nextForm); // preserve input regardless of outcome
    setSubmitting(true);
    const body = formToSubmission(nextForm);
    const res = await submitQuote(tokenRef.current, body);
    setSubmitting(false);
    if (res.ok) {
      setResult(res.data);
      setPhase("submitted");
    } else if ("closed" in res && res.closed) {
      setPhase("closed");
    } else {
      setPhase("soft-error");
    }
  }, []);

  if (!mounted || phase === "loading") {
    return <QuoteLoading />;
  }

  if (phase === "rejected") {
    return (
      <QuoteShell>
        <section className="quote-state-card" role="alert">
          <h1 className="quote-state-title">This link is no longer valid</h1>
          <p className="quote-state-body">
            If you received a quote request from {BRAND_NAME}, reply to that
            email and we&apos;ll send you a fresh link.
          </p>
        </section>
      </QuoteShell>
    );
  }

  if (phase === "closed") {
    return (
      <QuoteShell>
        <section className="quote-state-card">
          <h1 className="quote-state-title">This request has closed</h1>
          <p className="quote-state-body">
            The buyer&apos;s request this link belonged to is no longer open.
            No action is needed — we&apos;ll reach out the next time a request
            matches what you supply.
          </p>
        </section>
      </QuoteShell>
    );
  }

  if (phase === "submitted" && result) {
    return (
      <QuoteShell supplier={context?.supplier}>
        <section className="quote-state-card quote-state-card--confirm">
          <h1 className="quote-state-title">Quote received</h1>
          {result.status === "active" ? (
            <p className="quote-state-body">
              Your quote is in front of the buyer now. If they proceed,
              you&apos;ll hear from us at the contact on file.
            </p>
          ) : (
            <p className="quote-state-body">
              Thanks — your quote is in. It&apos;s being double-checked on our
              side before it goes live to the buyer
              {result.pn_differs
                ? " because the part number you quoted differs from the one requested (alternatives are welcome — we just confirm them first)"
                : ""}
              . No action is needed.
            </p>
          )}
        </section>
        {result.claim_pitch && (
          <section className="quote-claim-pitch">
            <p className="quote-claim-pitch-eyebrow">One more thing</p>
            <h2 className="quote-claim-pitch-title">
              Want to see how this quote does — and what other requests match
              you?
            </h2>
            <p className="quote-state-body">
              Claim your free {BRAND_NAME} supplier profile to track your
              quotes and get matched to more requests like this one. Reply to
              the quote-request email with &quot;claim my profile&quot; and
              we&apos;ll send your personal link.
            </p>
          </section>
        )}
      </QuoteShell>
    );
  }

  // ready OR soft-error: the request hero + the form (input preserved).
  if (context && form && context.request) {
    return (
      <QuoteShell supplier={context.supplier}>
        <section className="quote-request-card">
          <p className="quote-request-eyebrow">Quote request</p>
          <h1 className="quote-request-part">
            {[context.request.manufacturer, context.request.part_number]
              .filter(Boolean)
              .join(" — ") || "Requested part"}
          </h1>
          <dl className="quote-request-facts">
            {context.request.quantity != null && (
              <div className="quote-request-fact">
                <dt>Quantity</dt>
                <dd>{context.request.quantity}</dd>
              </div>
            )}
            {context.request.need_by && (
              <div className="quote-request-fact">
                <dt>Needed by</dt>
                <dd>{context.request.need_by}</dd>
              </div>
            )}
          </dl>
          {context.existing_quote && (
            <p className="quote-revision-note">
              You quoted ${context.existing_quote.unit_price} earlier —
              submitting again replaces that quote.
            </p>
          )}
        </section>
        {phase === "soft-error" && (
          <div className="portal-soft-error" role="alert" aria-live="polite">
            We couldn&apos;t submit your quote right now. Your entries are kept
            — please try again in a moment.
          </div>
        )}
        <QuoteForm
          initial={form}
          requestedPartNumber={context.request.part_number}
          onChange={setForm}
          onSubmit={handleSubmit}
          submitting={submitting}
        />
      </QuoteShell>
    );
  }

  return <QuoteLoading />;
}

// ---------------------------------------------------------------------------
// Layout shells (the portal-surface palette — one visual system)
// ---------------------------------------------------------------------------

function QuoteShell({
  supplier,
  children,
}: {
  supplier?: { name: string | null; domain: string };
  children: React.ReactNode;
}) {
  return (
    <div className="portal-surface">
      <header className="portal-header">
        <span className="portal-brand">{BRAND_NAME}</span>
        {supplier && (
          <span className="portal-supplier" title={supplier.domain}>
            {supplier.name || supplier.domain}
          </span>
        )}
      </header>
      <main className="portal-main">{children}</main>
      <footer className="portal-footer">
        {BRAND_NAME} never asks for payment or credentials over chat.
      </footer>
    </div>
  );
}

function QuoteLoading() {
  return (
    <div className="portal-surface portal-loading">
      <GoferLoader size={120} aria-label="Loading the quote request" />
      <p className="portal-loading-text">Loading the request…</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// mappers: token context <-> form state
// ---------------------------------------------------------------------------

function contextToForm(ctx: QuoteContext): QuoteFormState {
  return {
    quoteNumber: "",
    unitPrice: "",
    quantity: ctx.request?.quantity != null ? String(ctx.request.quantity) : "",
    leadTimeDays: "",
    inStock: false,
    partNumber: ctx.request?.part_number ?? "",
    freight: "",
    validUntil: "",
    notes: "",
  };
}

function formToSubmission(f: QuoteFormState): QuoteSubmissionBody {
  return {
    quote_number: f.quoteNumber.trim(),
    unit_price: Number(f.unitPrice),
    quantity: Number(f.quantity),
    lead_time: f.inStock ? "in stock" : `${f.leadTimeDays.trim()} days`,
    part_number: f.partNumber.trim() || null,
    freight: f.freight.trim() || null,
    valid_until: f.validUntil || null,
    notes: f.notes.trim() || null,
  };
}
