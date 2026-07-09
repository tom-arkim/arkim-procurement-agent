/**
 * Customer-facing brand identity — single source of truth.
 *
 * The customer-facing brand name is "Gofer" (USPTO Class 35 cleared + the
 * gofer.ai / goferai.com ownership check passed). Every customer-facing
 * component reads BRAND_NAME from this module — do not hard-code the brand
 * string anywhere else. If the name ever changes again, swap it here in ONE
 * place.
 *
 * Backend mirror: utils/supplier_portal.py `_ZERO_STATE_FRAMING` carries the
 * same brand name inline ("...so Gofer can match you..."). Keep the two in
 * sync — the zero-state teaser text must match the UI.
 */
export const BRAND_NAME = "Gofer";
