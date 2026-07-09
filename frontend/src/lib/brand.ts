/**
 * Customer-facing brand identity — single source of truth.
 *
 * The supplier claim-portal is the FIRST surface a real supplier sees, so the
 * brand name on it matters. The name is unsettled: "Gofer" / "Gofer AI" is the
 * direction but is pending a USPTO Class 35 search and the gofer.ai / goferai.com
 * ownership check — NOT yet cleared. Until it settles, the customer-facing name
 * stays the current value (Arkim) so the portal never shows an uncleared name.
 *
 * Swap the name here in ONE place when it clears; every portal component reads
 * BRAND_NAME from this module. Do not hard-code the brand string anywhere else.
 *
 * FOLLOW-UP (backend, same swap motion): utils/supplier_portal.py
 * `_ZERO_STATE_FRAMING` carries the same brand name inline ("...so Arkim can
 * match you..."). When the name settles, update that backend string in the same
 * change so the zero-state teaser text stays consistent with the UI. Tracked in
 * the build brief's BRAND-STRING NOTE.
 */
export const BRAND_NAME = "Arkim";
