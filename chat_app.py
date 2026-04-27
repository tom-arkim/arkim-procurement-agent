"""
Arkim Procurement Agent — Streamlit Web Application
"""
import sys, os, json, re, base64, io
import requests

# Ensure emoji in print() calls don't crash on Windows cp1252
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
import pandas as pd
import streamlit as st

# ── Bootstrap path ────────────────────────────────────────────────────────────
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="Arkim · Procurement AI",
    page_icon="⚙",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  /* ── Cards ── */
  .a-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 1rem;
  }
  .a-label {
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: #58a6ff;
    font-weight: 700;
    margin-bottom: .6rem;
  }
  /* ── Chips ── */
  .chip {
    display: inline-block;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 5px;
    padding: .12rem .5rem;
    font-size: .78rem;
    margin: .12rem;
    font-family: 'SFMono-Regular', monospace;
    color: #c9d1d9;
  }
  .chip-b { border-color: #58a6ff; color: #58a6ff; }
  .chip-g { border-color: #3fb950; color: #3fb950; }
  .chip-r { border-color: #f85149; color: #f85149; }
  .chip-a { border-color: #d29922; color: #d29922; }
  /* ── Metrics ── */
  .price-xl  { font-size: 2rem;   font-weight: 800; color: #3fb950; line-height: 1.1; }
  .tca-xl    { font-size: 2.4rem; font-weight: 800; color: #58a6ff; line-height: 1.1; }
  .m-label   { font-size: .7rem;  text-transform: uppercase; letter-spacing: .08em; color: #8b949e; margin-bottom: .2rem; }
  /* ── Recommended banner ── */
  .rec-bar {
    background: linear-gradient(90deg, #1a7f37, #2ea043);
    border-radius: 6px;
    padding: .5rem .9rem;
    font-weight: 700;
    color: #fff;
    font-size: .88rem;
    margin-bottom: .75rem;
  }
  /* ── Page header ── */
  .page-hdr {
    background: linear-gradient(135deg, #0c2a6e 0%, #0d419d 55%, #1c54cc 100%);
    padding: 1.1rem 1.6rem;
    border-radius: 10px;
    border: 1px solid #1c4dbf;
    margin-bottom: 1.5rem;
  }
  .page-hdr h1 { margin: 0; color: #fff; font-size: 1.4rem; font-weight: 800; }
  .page-hdr p  { margin: .2rem 0 0; color: #93b8f9; font-size: .8rem; }
  /* ── Sidebar section labels ── */
  .sb-sec {
    font-size: .66rem;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: #8b949e;
    margin: .9rem 0 .35rem;
    border-bottom: 1px solid #30363d;
    padding-bottom: .25rem;
  }
  /* ── Command center header ── */
  .cmd-hdr {
    background: linear-gradient(135deg, #0c2a6e 0%, #0d419d 55%, #1c54cc 100%);
    padding: 1.4rem 2rem;
    border-radius: 12px;
    border: 1px solid #1c4dbf;
    margin-bottom: 1.2rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .cmd-hdr h1 { margin: 0; color: #fff; font-size: 1.5rem; font-weight: 900; letter-spacing: -.02em; }
  .cmd-hdr p  { margin: .2rem 0 0; color: #93b8f9; font-size: .8rem; }
  .live-badge {
    background: rgba(63,185,80,.15);
    border: 1px solid #3fb950;
    border-radius: 20px;
    padding: .3rem .9rem;
    font-size: .7rem;
    font-weight: 700;
    color: #3fb950;
    letter-spacing: .08em;
    text-transform: uppercase;
  }
  /* ── Metrics bar ── */
  [data-testid="metric-container"] {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 1rem 1.2rem;
  }
  [data-testid="stMetricLabel"] > div {
    font-size: .68rem !important;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #8b949e !important;
    font-weight: 700;
  }
  [data-testid="stMetricValue"] {
    font-size: 1.75rem !important;
    font-weight: 800 !important;
    color: #e6edf3 !important;
  }
  [data-testid="stMetricDelta"] { font-size: .75rem !important; }
  /* ── Sidebar logo ── */
  .sb-logo { padding: .3rem 0 .9rem; border-bottom: 1px solid #30363d; margin-bottom: .6rem; }
  .sb-logo .logo-t { font-size: 1.35rem; font-weight: 900; color: #fff; letter-spacing: -.02em; }
  .sb-logo .logo-s { font-size: .65rem; color: #58a6ff; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; margin-top: .05rem; }
  /* ── Empty state ── */
  .empty-state {
    text-align: center;
    padding: 3rem 2rem;
    border: 1px dashed #30363d;
    border-radius: 12px;
    margin: 1rem 0;
  }
  .empty-state .es-icon  { font-size: 2.2rem; margin-bottom: .6rem; }
  .empty-state .es-title { font-size: 1rem; font-weight: 700; color: #e6edf3; margin-bottom: .3rem; }
  .empty-state .es-body  { font-size: .84rem; color: #8b949e; line-height: 1.6; max-width: 380px; margin: 0 auto; }
  /* ── Tab strip ── */
  .stTabs [data-baseweb="tab-list"] {
    gap: 3px;
    background: #161b22;
    border-radius: 8px;
    padding: 4px;
    border: 1px solid #30363d;
  }
  .stTabs [data-baseweb="tab"] { border-radius: 6px; font-size: .82rem; font-weight: 600; color: #8b949e; }
  .stTabs [aria-selected="true"] { background: #21262d !important; color: #e6edf3 !important; }
  /* ── Quote cards ── */
  .q-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: .9rem 1.1rem;
    margin-bottom: .6rem;
  }
  .q-card-best { border-color: #d29922; box-shadow: 0 0 0 1px rgba(210,153,34,.25); }
  .best-badge {
    display: inline-block;
    background: rgba(210,153,34,.15);
    border: 1px solid #d29922;
    border-radius: 4px;
    padding: .06rem .4rem;
    font-size: .62rem;
    font-weight: 700;
    color: #d29922;
    letter-spacing: .06em;
    text-transform: uppercase;
    vertical-align: middle;
  }
  .rfq-warning {
    display: inline-flex;
    align-items: center;
    gap: .3rem;
    background: rgba(248,81,73,.1);
    border: 1px solid rgba(248,81,73,.35);
    border-radius: 4px;
    padding: .12rem .45rem;
    font-size: .7rem;
    color: #f85149;
    font-weight: 600;
    margin-top: .35rem;
  }
  /* ── Confirmed screen ── */
  .confirmed-banner {
    background: linear-gradient(135deg, #1a7f37 0%, #2ea043 100%);
    border-radius: 12px;
    border: 1px solid #3fb950;
    padding: 1.6rem 2rem;
    text-align: center;
    margin-bottom: 1.2rem;
  }
  .confirmed-banner h2 { margin: 0 0 .35rem; color: #fff; font-size: 1.45rem; font-weight: 900; }
  .confirmed-banner p  { margin: 0; color: rgba(255,255,255,.82); font-size: .88rem; }
  /* ── Radio-as-tabs ── */
  div[data-testid="stRadio"] { margin-bottom: .75rem; }
  div[data-testid="stRadio"] > div {
    display: flex !important;
    gap: 3px;
    background: #161b22;
    border-radius: 8px;
    padding: 4px;
    border: 1px solid #30363d;
    flex-direction: row !important;
  }
  div[data-testid="stRadio"] label {
    border-radius: 6px !important;
    padding: .38rem .9rem !important;
    font-size: .82rem !important;
    font-weight: 600 !important;
    color: #8b949e !important;
    cursor: pointer;
    flex: 1;
    text-align: center;
  }
  div[data-testid="stRadio"] label:has(input:checked) {
    background: #21262d !important;
    color: #e6edf3 !important;
  }
  div[data-testid="stRadio"] label > div:first-child { display: none !important; }
  /* ── Draft card ── */
  .draft-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-left: 3px solid #d29922;
    border-radius: 8px;
    padding: .75rem 1rem;
    margin-bottom: .5rem;
  }
  /* ── Tier headers ── */
  .tier3-hdr {
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .1em;
    color: #d29922;
    font-weight: 700;
    margin: 1.2rem 0 .5rem;
    padding-bottom: .3rem;
    border-bottom: 1px solid #30363d;
  }
  .tier-hdr {
    font-size: .7rem;
    text-transform: uppercase;
    letter-spacing: .1em;
    font-weight: 700;
    margin: 1rem 0 .4rem;
    padding-bottom: .3rem;
    border-bottom: 1px solid #30363d;
  }
  .tier-hdr-1 { color: #3fb950; border-color: #3fb95040; }
  .tier-hdr-2 { color: #58a6ff; border-color: #58a6ff40; }
  /* ── Partner badges ── */
  .badge-gold {
    display: inline-block;
    background: rgba(210,153,34,.18);
    border: 1px solid #d29922;
    border-radius: 4px;
    padding: .04rem .38rem;
    font-size: .62rem;
    font-weight: 700;
    color: #d29922;
    letter-spacing: .06em;
    text-transform: uppercase;
    vertical-align: middle;
    margin-left: .3rem;
  }
  .badge-silver {
    display: inline-block;
    background: rgba(139,148,158,.15);
    border: 1px solid #8b949e;
    border-radius: 4px;
    padding: .04rem .38rem;
    font-size: .62rem;
    font-weight: 700;
    color: #8b949e;
    letter-spacing: .06em;
    text-transform: uppercase;
    vertical-align: middle;
    margin-left: .3rem;
  }
  /* ── Suitability score pill ── */
  .suit-hi  { color: #3fb950; font-weight: 700; font-size: .78rem; }
  .suit-mid { color: #d29922; font-weight: 700; font-size: .78rem; }
  .suit-lo  { color: #8b949e; font-weight: 700; font-size: .78rem; }
  /* ── Tier 3 vendor cards ── */
  .t3-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: .7rem 1rem;
    margin-bottom: .45rem;
    display: flex;
    align-items: flex-start;
    gap: .9rem;
  }
  .t3-suit-col {
    min-width: 54px;
    text-align: center;
    padding-top: .1rem;
  }
  .t3-suit-num {
    font-size: 1.1rem;
    font-weight: 800;
    line-height: 1;
  }
  .t3-suit-lbl {
    font-size: .58rem;
    text-transform: uppercase;
    letter-spacing: .07em;
    color: #8b949e;
  }
  .t3-body { flex: 1; min-width: 0; }
  .t3-name { font-size: .92rem; font-weight: 700; color: #e6edf3; }
  .t3-meta { font-size: .74rem; color: #8b949e; margin-top: .2rem; }
  /* ── MoR badge ── */
  .mor-badge {
    display: inline-block;
    background: rgba(88,166,255,.12);
    border: 1px solid #58a6ff;
    border-radius: 4px;
    padding: .04rem .38rem;
    font-size: .6rem;
    font-weight: 700;
    color: #58a6ff;
    letter-spacing: .06em;
    text-transform: uppercase;
    vertical-align: middle;
    margin-left: .3rem;
  }
  /* ── Workflow selector ── */
  .wf-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: .8rem 1rem;
    margin-bottom: .6rem;
    cursor: pointer;
    transition: border-color .15s;
  }
  .wf-card-active { border-color: #58a6ff; background: rgba(88,166,255,.07); }
  .wf-title { font-size: .9rem; font-weight: 700; color: #e6edf3; }
  .wf-sub   { font-size: .73rem; color: #8b949e; margin-top: .15rem; }
  /* ── Warranty badge ── */
  .warranty-active  { color: #3fb950; font-weight: 700; font-size: .78rem; }
  .warranty-expired { color: #f85149; font-weight: 700; font-size: .78rem; }
  .warranty-unknown { color: #8b949e; font-size: .78rem; }
  /* ── TLV metric ── */
  .tlv-xl { font-size: 1.5rem; font-weight: 800; color: #d29922; line-height: 1.1; }
  /* ── Misc ── */
  #MainMenu, footer, header { visibility: hidden; }
  code { background: #21262d; border-radius: 4px; padding: .1rem .3rem; }
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "messages":           [],
    "specs":              None,
    "all_options":        [],
    "all_quotes":         [],
    "best_quote":         None,
    "rfq_draft":          None,
    "rfq_emails":         {},       # {vendor_name: email_text} — generated per-vendor
    "inventory_hit":      False,
    "inventory_location": None,
    "site":               "La Mirada",
    "pipeline_ran":       False,
    "pipeline_error":     None,
    "pending_run":          None,   # {"specs": AssetSpecs, "site": str}
    "pending_image":        None,   # {"bytes": bytes, "filename": str, "site": str}
    "accepted_quote":       None,   # ArkimQuote accepted by user
    "order_history":        [],     # list of accepted order dicts
    "rfq_vendors_selected": [],
    "rfq_campaign_sent":    False,
    "active_tab":           "🔍 Active Sourcing",
    "sourcing_history":           [],    # auto-saved sourcing events
    "force_refresh":              False, # bypass price cache on next pipeline run
    "pending_verification_specs": None,  # specs waiting for user to confirm missing fields
    "pending_verification_site":  None,
    "search_mode":                "exact",  # "exact" | "equivalents" — carried into pipeline
    "workflow_mode":              "spare_parts",  # "spare_parts" | "replacement" | "capex"
    "downtime_cost_per_day":      500.0,   # USD/day — used for TLV calculation
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLM helpers — direct HTTP (no Anthropic SDK needed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_APP_EXTRACTION_MODEL = os.environ.get("OS_EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
_APP_VISION_MODEL     = os.environ.get("OS_VISION_MODEL",     "claude-sonnet-4-6")


def _claude(system: str, user: str, api_key: str,
            model: str = _APP_EXTRACTION_MODEL,
            max_tokens: int = 1024) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()


def _detect_media_type(image_bytes: bytes) -> str:
    """Detect image format from magic bytes, not filename."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # assume JPEG as safe fallback


def _compress_image(image_bytes: bytes, media_type: str, max_bytes: int = 3_500_000) -> tuple[bytes, str]:
    """Resize image proportionally until it fits under max_bytes."""
    if len(image_bytes) <= max_bytes:
        return image_bytes, media_type
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        quality = 85
        scale = 1.0
        while True:
            buf = io.BytesIO()
            w = int(img.width * scale)
            h = int(img.height * scale)
            resized = img.resize((w, h), Image.LANCZOS) if scale < 1.0 else img
            resized.save(buf, format="JPEG", quality=quality)
            data = buf.getvalue()
            if len(data) <= max_bytes or quality < 40:
                return data, "image/jpeg"
            quality -= 15
            if quality < 40:
                scale *= 0.75
                quality = 75
    except Exception:
        return image_bytes, media_type


def _claude_vision(image_bytes: bytes, media_type: str,
                   prompt: str, api_key: str) -> str:
    image_bytes, media_type = _compress_image(image_bytes, media_type)
    b64 = base64.b64encode(image_bytes).decode()
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _APP_VISION_MODEL,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {"type": "text", "text": prompt},
            ]}],
        },
        timeout=60,
    )
    if not resp.ok:
        try:
            err = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            err = resp.text
        raise RuntimeError(f"Anthropic API error {resp.status_code}: {err}")
    return resp.json()["content"][0]["text"].strip()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pipeline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _patch_sourcing_keys(tavily_key: str, anthropic_key: str) -> None:
    import utils.sourcing as sm
    try:
        from tavily import TavilyClient as _TC
    except ImportError:
        from tavily import Client as _TC
    if tavily_key and tavily_key != sm.TAVILY_API_KEY:
        sm.TAVILY_API_KEY = tavily_key
        sm._tavily = _TC(api_key=tavily_key)
    if anthropic_key:
        sm.ANTHROPIC_API_KEY = anthropic_key


def _execute_pipeline(specs, site: str,
                      tavily_key: str, anthropic_key: str,
                      force_refresh: bool = False,
                      search_mode: str = "exact",
                      workflow: str = "spare_parts",
                      downtime_cost_per_day: float = 500.0) -> None:
    from utils.inventory import check_internal
    from utils.sourcing  import find_vendors
    from utils.quoting   import generate_arkim_quote
    from datetime import datetime as _dt
    _patch_sourcing_keys(tavily_key, anthropic_key)

    wf_labels = {
        "spare_parts": "Spare Parts (OpEx) — Exact PN",
        "replacement":  "Replacement Equipment — Interchangeability",
        "capex":        "New Equipment (CapEx) — Proposal Requests",
    }
    with st.status(f"Running Arkim Pipeline · {wf_labels.get(workflow, workflow)}…", expanded=True) as status:
        st.write("🔍 **Scanning** internal inventory…")
        hit, location, _ = check_internal(specs)
        st.session_state.inventory_hit      = hit
        st.session_state.inventory_location = location
        st.write(
            f"✅ In-stock at **{location}** — will compare"
            if hit else
            "❌ Not in inventory — proceeding to external sourcing"
        )

        if workflow == "capex":
            st.write("📋 **CapEx Mode** — skipping Tier 1/2 price search, discovering specialist vendors…")
        else:
            src_label  = "fetching fresh prices" if force_refresh else "checking price DB first"
            mode_label = "exact PN" if search_mode == "exact" else "exact + equivalents"
            st.write(f"🌐 **Sourcing** Grainger · McMaster-Carr · MSC ({src_label} · {mode_label})…")

        st.write("📦 **Batching Snippets** — parsing vendor results…")
        options, _ = find_vendors(specs, site=site, force_refresh=force_refresh,
                                  search_mode=search_mode, workflow=workflow)
        st.write("🔒 **Validating Magnitude** — applying freight & weight guards…")
        st.session_state.all_options = options
        st.session_state.rfq_draft   = None
        st.session_state.rfq_emails  = {}

        national_priced = sum(1 for o in options
                              if o.merchant_type in ("Enterprise", "National Specialist")
                              and not getattr(o, "price_tbd", False))
        national_inq    = sum(1 for o in options
                              if o.merchant_type in ("Enterprise", "National Specialist")
                              and getattr(o, "price_tbd", False))
        tbd_count       = sum(1 for o in options if getattr(o, "price_tbd", False))

        if national_priced == 0 and national_inq == 0:
            st.write(
                f"⚠️ No direct buy pricing found — "
                f"{tbd_count} specialist(s) queued for Tier 3 Partner Outreach."
            )
        else:
            inq_note = f" · **{national_inq}** inquiry-required" if national_inq else ""
            st.write(f"✅ **{national_priced}** priced{inq_note}")

        priced_options = [o for o in options if not getattr(o, "price_tbd", False)]
        all_quotes: list = []

        if not priced_options:
            st.session_state.all_quotes     = []
            st.session_state.best_quote     = None
            st.session_state.specs          = specs
            st.session_state.site           = site
            st.session_state.pipeline_ran   = True
            st.session_state.pipeline_error = None
        else:
            metric = "TCA" if workflow == "spare_parts" else "TLV"
            st.write(f"📊 **Calculating TLV** — purchase price + downtime risk + shipping + tax…")
            all_quotes, best = generate_arkim_quote(
                specs, priced_options, site=site,
                workflow=workflow, downtime_cost_per_day=downtime_cost_per_day,
            )
            st.session_state.all_quotes    = all_quotes
            st.session_state.best_quote    = best
            st.session_state.specs         = specs
            st.session_state.site          = site
            st.session_state.pipeline_ran  = True
            st.session_state.pipeline_error = None
            if workflow == "spare_parts":
                score_str = f"TCA {best.tca_score:.0f}/100"
            else:
                score_str = f"TLV ${best.tlv_score:,.2f}"
            st.write(
                f"✅ Recommended: **{best.chosen_option.vendor_name}** — "
                f"Arkim **${best.arkim_sale_price:.2f}** + tax **${best.tax_amount:.2f}** = "
                f"Grand Total **${best.grand_total:.2f}** | {score_str}"
            )

        # Pre-check all Tier 3 vendor checkboxes (user can deselect)
        for k in list(st.session_state.keys()):
            if k.startswith("rfq_chk_"):
                del st.session_state[k]
        for o in options:
            if o.requires_rfq:
                st.session_state[f"rfq_chk_{o.vendor_name}"] = True

        # Auto-save full sourcing event to history
        entry_label = f"{specs.manufacturer} {specs.model} ({specs.part_number})"
        entry = {
            "label":      entry_label,
            "specs":      specs,
            "all_quotes": all_quotes,
            "all_options":options,
            "site":       site,
            "workflow":   workflow,
            "saved_at":   _dt.now().strftime("%Y-%m-%d %H:%M"),
        }
        history = st.session_state.sourcing_history
        labels  = [h["label"] for h in history]
        if entry_label in labels:
            history[labels.index(entry_label)] = entry
        else:
            history.insert(0, entry)
        st.session_state.sourcing_history = history

        status.update(label="✅ Pipeline complete", state="complete", expanded=False)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Intent classification + spec extraction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_INTENT_SYS = """You are a classifier for Arkim, an industrial procurement AI.

Classify the user message and return ONLY JSON (no prose):
{
  "intent":        "SOURCING" | "FOLLOW_UP" | "GENERAL" | "SPEC_CLARIFICATION",
  "category":      "Part" | "Equipment",
  "detected_type": string or null,
  "manufacturer":  string or null,
  "model":         string or null,
  "part_number":   string or null,
  "voltage":       string or null,
  "phase":         string or null,
  "hp":            string or null,
  "gpm":           string or null,
  "psi":           string or null,
  "frame":         string or null,
  "description":   string or null,
  "site":          string or null
}

Intent rules:
  SOURCING           = user wants to find, source, buy, or quote a part or piece of equipment.
  SPEC_CLARIFICATION = user is providing additional specs/details (voltage, phase, GPM, etc.)
                       in response to a previous question — NOT a new sourcing request.
  FOLLOW_UP          = question about previously shown procurement results.
  GENERAL            = greeting, thanks, or unrelated.

Category rules:
  "Equipment" = full assembled unit (pump, motor, compressor, blower, drive unit, fan, conveyor).
  "Part"      = replacement component (starter, VFD, bearing, seal, relay, contactor, sensor).

detected_type: a precise, brand-agnostic searchable category, e.g.
  "Vertical Multi-Stage Centrifugal Pump", "3-Phase TEFC Induction Motor",
  "Variable Frequency Drive", "Horizontal Belt Conveyor", "Magnetic Motor Starter".

Return ONLY valid JSON.
"""


def classify_message(text: str, api_key: str) -> dict:
    raw = _claude(_INTENT_SYS, text, api_key)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"intent": "GENERAL"}


_VISION_PROMPT = """This is an industrial equipment nameplate photo.
Read all visible text and return ONLY JSON (no prose):
{
  "category":      "Part" or "Equipment",
  "detected_type": "precise brand-agnostic type, e.g. Vertical Multi-Stage Centrifugal Pump",
  "manufacturer":  "...",
  "model":         "...",
  "part_number":   "...",
  "voltage":       "...",
  "phase":         "3-phase or single-phase or null",
  "hp":            "...",
  "serial_number": "...",
  "description":   "brief one-line description",
  "gpm":           "...",
  "psi":           "...",
  "frame":         "..."
}

Category: "Equipment" = full unit (pump, motor, compressor, blower, fan, conveyor).
          "Part"      = replacement component (relay, contactor, VFD, bearing, seal, sensor).
phase / gpm / psi / frame: extract even if brand is unreadable. Use null for any field not visible.
"""


def specs_from_image(image_bytes: bytes, filename: str, api_key: str):
    from utils.models import AssetSpecs
    media_type = _detect_media_type(image_bytes)
    raw = _claude_vision(image_bytes, media_type, _VISION_PROMPT, api_key)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        d   = json.loads(m.group(0))
        cat = d.get("category", "Part")
        if cat not in ("Part", "Equipment"):
            cat = "Part"
        return AssetSpecs(
            manufacturer=d.get("manufacturer") or "Unknown",
            model=d.get("model") or "Unknown",
            part_number=d.get("part_number") or "UNKNOWN-PN",
            voltage=d.get("voltage") or "N/A",
            category=cat,
            hp=d.get("hp"),
            serial_number=d.get("serial_number"),
            description=d.get("description"),
            raw_text=raw,
            gpm=d.get("gpm"),
            psi=d.get("psi"),
            frame=d.get("frame"),
            phase=d.get("phase"),
            detected_type=d.get("detected_type"),
            rpm=d.get("rpm"),
        )
    from utils.vision import extract_specs
    return extract_specs(filename)


def specs_from_classification(classified: dict):
    from utils.models import AssetSpecs
    mfg = classified.get("manufacturer")
    mdl = classified.get("model")
    pn  = classified.get("part_number")
    cat = classified.get("category", "Part")
    if cat not in ("Part", "Equipment"):
        cat = "Part"
    if mfg or mdl or pn or classified.get("gpm") or classified.get("psi"):
        return AssetSpecs(
            manufacturer=mfg or "Unknown",
            model=mdl or "Unknown",
            part_number=pn or "UNKNOWN-PN",
            voltage=classified.get("voltage") or "N/A",
            category=cat,
            hp=classified.get("hp"),
            description=classified.get("description") or "",
            raw_text=str(classified),
            gpm=classified.get("gpm"),
            psi=classified.get("psi"),
            frame=classified.get("frame"),
            phase=classified.get("phase"),
            detected_type=classified.get("detected_type"),
            rpm=classified.get("rpm"),
        )
    from utils.vision import extract_specs
    return extract_specs(str(classified))



def _missing_critical_specs(specs) -> list[str]:
    """Return human-readable names of critical specs that are missing/unknown."""
    _null = {"N/A", "Unknown", "null", "None", "UNKNOWN-PN", None, ""}
    missing = []
    if specs.voltage in _null:
        missing.append("Voltage")
    if getattr(specs, "phase", None) in _null and specs.category == "Equipment":
        missing.append("Phase (single-phase or 3-phase)")
    # For Equipment, at least one performance spec must be known
    if specs.category == "Equipment":
        has_perf = any([
            specs.hp    and specs.hp    not in _null,
            getattr(specs, "gpm",   None) not in _null,
            getattr(specs, "psi",   None) not in _null,
            getattr(specs, "frame", None) not in _null,
        ])
        if not has_perf:
            missing.append("at least one performance spec (HP, GPM, PSI, or Frame)")
    # Motor-specific: flag missing Frame + RPM together (needed for equivalence search)
    if getattr(specs, "missing_critical_specs", False):
        missing.append("Frame size and RPM (required for motor equivalence matching)")
    return missing


def _merge_spec_clarification(base_specs, classified: dict):
    """Merge non-null fields from a SPEC_CLARIFICATION classification into existing specs."""
    _null = {"N/A", "Unknown", "null", "None", None, ""}
    for field in ("voltage", "phase", "hp", "gpm", "psi", "frame", "detected_type", "description"):
        val = classified.get(field)
        if val and val not in _null:
            setattr(base_specs, field, val)
    return base_specs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Conversational follow-up
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_context() -> str:
    lines = []
    s = st.session_state.specs
    if s:
        cat  = getattr(s, "category", "Part")
        tech = " | ".join(filter(None, [
            s.voltage if s.voltage not in ("N/A", "null") else None,
            s.hp      if s.hp and s.hp not in ("N/A","None","null") else None,
            f"{s.gpm} GPM" if getattr(s,"gpm",None) else None,
            f"{s.psi} PSI" if getattr(s,"psi",None) else None,
            f"Frame {s.frame}" if getattr(s,"frame",None) else None,
            getattr(s,"rpm",None) if getattr(s,"rpm",None) else None,
        ]))
        lines.append(f"Current {cat}: {s.manufacturer} {s.model} ({s.part_number}) | {tech}")
    lines.append(
        f"Inventory: IN STOCK at {st.session_state.inventory_location}"
        if st.session_state.inventory_hit
        else "Inventory: NOT IN STOCK"
    )
    for q in st.session_state.all_quotes:
        o = q.chosen_option
        tag = " [+$50 RFQ]" if o.requires_rfq else ""
        lines.append(
            f"  {o.vendor_name} ({o.merchant_type}): vendor ${o.base_price:.2f} "
            f"→ Arkim ${q.arkim_sale_price:.2f} + tax ${q.tax_amount:.2f} "
            f"= Grand Total ${q.grand_total:.2f} | {o.lead_time_days}d | TCA {q.tca_score:.1f}{tag}"
        )
    if st.session_state.rfq_emails:
        lines.append(f"[RFQ emails generated for: {', '.join(st.session_state.rfq_emails.keys())} — reproduce in full if requested]")

    if len(st.session_state.sourcing_history) > 1:
        lines.append("\nPrevious Sourcing Runs:")
        for d in st.session_state.sourcing_history[1:]:  # skip current (index 0)
            best = d["all_quotes"][0] if d["all_quotes"] else None
            if best:
                lines.append(
                    f"  HISTORY: {d['label']} @ {d['site']} — "
                    f"best offer: {best.chosen_option.vendor_name} ${best.grand_total:.2f} "
                    f"| TCA {best.tca_score:.1f} | {d['saved_at']}"
                )

    if st.session_state.order_history:
        lines.append("\nConfirmed Orders:")
        for o in st.session_state.order_history:
            lines.append(
                f"  ORDER: {o['Asset']} @ {o['Site']} — "
                f"{o['Vendor']} ${o['Grand Total']:.2f} | accepted {o['Accepted']} | {o['Status']}"
            )

    return "\n".join(lines) or "No sourcing data yet."


_CHAT_SYS = """You are Arkim's procurement AI assistant — sharp, data-driven, and concise.
You help industrial buyers make fast, cost-effective sourcing decisions.

Current procurement context:
{ctx}

Answer using the context above. Reference specific vendors, prices, lead times, and TCA scores.
If asked about faster shipping, identify the lowest lead-time option.
If asked for the RFQ email, reproduce it in full.
Keep responses under 200 words unless reproducing an email or table.
"""


def chat_respond(message: str, api_key: str) -> str:
    if st.session_state.rfq_draft and re.search(r"\brfq\b|email|draft|outreach", message, re.I):
        return f"Here's the RFQ email draft:\n\n```\n{st.session_state.rfq_draft}\n```"
    ctx = _build_context()
    return _claude(_CHAT_SYS.format(ctx=ctx), message, api_key, max_tokens=600)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dashboard renderers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def render_asset_card() -> None:
    s   = st.session_state.specs
    hit = st.session_state.inventory_hit
    loc = st.session_state.inventory_location
    inv = (
        f'<span class="chip chip-g">&#10003; IN STOCK &middot; {loc}</span>'
        if hit else
        '<span class="chip chip-r">&#10007; NOT IN STOCK</span>'
    )
    cat     = getattr(s, "category", "Part")
    cat_cls = "chip-a" if cat == "Equipment" else "chip-b"
    cat_chip = f'<span class="chip {cat_cls}" style="font-size:.65rem;">{cat.upper()}</span>'

    hp_chip    = f'<span class="chip">{s.hp}</span>'      if s.hp    and s.hp    not in ("N/A","None","null") else ""
    sn_chip    = f'<span class="chip">SN: {s.serial_number}</span>' if s.serial_number and s.serial_number != "N/A" else ""
    gpm_chip   = f'<span class="chip">{s.gpm} GPM</span>' if getattr(s,"gpm",None) else ""
    psi_chip   = f'<span class="chip">{s.psi} PSI</span>' if getattr(s,"psi",None) else ""
    frame_chip = f'<span class="chip">Frame {s.frame}</span>' if getattr(s,"frame",None) else ""
    phase_chip = f'<span class="chip">{s.phase}</span>' if getattr(s,"phase",None) else ""
    dtype      = getattr(s, "detected_type", None)
    dtype_chip = f'<span class="chip chip-b" style="font-size:.65rem;">{dtype}</span>' if dtype else ""
    desc       = s.description or ""

    pn_display = f'# {s.part_number}' if s.part_number not in ("UNKNOWN-PN","Unknown") else "PN unknown"

    st.markdown(f"""
    <div class="a-card">
      <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem;">
        <div class="a-label" style="margin:0;">Asset Identified</div>
        {cat_chip}
      </div>
      <div style="display:flex;align-items:baseline;gap:.6rem;margin-bottom:.5rem;">
        <span style="font-size:1.25rem;font-weight:700;color:#e6edf3;">{s.manufacturer}</span>
        <span style="font-size:1rem;color:#8b949e;">{s.model}</span>
      </div>
      {dtype_chip}
      <span class="chip chip-b">{pn_display}</span>
      <span class="chip">{s.voltage}</span>
      {hp_chip}{phase_chip}{gpm_chip}{psi_chip}{frame_chip}{sn_chip}
      <div style="margin-top:.6rem;font-size:.82rem;color:#8b949e;">{desc}</div>
      <div style="margin-top:.6rem;">{inv}</div>
    </div>
    """, unsafe_allow_html=True)


def render_vendor_cards() -> None:
    quotes   = st.session_state.all_quotes
    all_opts = st.session_state.all_options
    tbd_count = sum(1 for o in all_opts if getattr(o, "price_tbd", False))

    if not quotes:
        # No priced options — show CTA pointing to Tier 3 section below
        if all_opts:
            st.markdown(f"""
            <div class="a-card" style="border-color:#f85149;background:rgba(248,81,73,.05);">
              <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.5rem;">
                <span style="font-size:1.1rem;">&#9888;</span>
                <span style="font-weight:700;color:#f85149;font-size:.95rem;">
                  No Direct Buy Pricing Available
                </span>
              </div>
              <div style="font-size:.82rem;color:#8b949e;line-height:1.6;">
                National distributors (Grainger, McMaster-Carr, MSC, Motion Industries,
                Applied Industrial, Pump Products, Zoro) returned no verified pricing for
                this item. This is common for specialty or high-configuration industrial equipment.
              </div>
              <div style="margin-top:.75rem;font-size:.82rem;color:#e6edf3;">
                <b>Next step:</b> Tier 3 Managed RFQ Outreach —
                <b style="color:#d29922;">{tbd_count} vendor(s)</b> found (price inquiry required).
                Review and initiate outreach below. Arkim contacts them on your behalf within
                <b>24–48 hours</b>.
              </div>
            </div>""", unsafe_allow_html=True)
        return

    wf    = st.session_state.workflow_mode
    cat   = getattr(st.session_state.specs, "category", "Part") if st.session_state.specs else "Part"
    if wf == "spare_parts":
        cmp_label = "Spare Parts — Ranked by TCA (Lead Time · Accuracy · Cost)"
    elif wf == "replacement":
        cmp_label = "Replacement Equipment — Ranked by TLV (Total Life Cycle Value)"
    else:
        cmp_label = "Vendor Comparison — TCA Ranked by Tier"
    st.markdown(f'<div class="a-label" style="margin:.4rem 0 .7rem;">{cmp_label}</div>', unsafe_allow_html=True)

    # Split into tiers for grouped display; global TCA rank preserved for best-badge
    tier1_quotes = [q for q in quotes if q.chosen_option.merchant_type == "Enterprise"]
    tier2_quotes = [q for q in quotes if q.chosen_option.merchant_type == "National Specialist"]
    other_quotes = [q for q in quotes if q.chosen_option.merchant_type
                    not in ("Enterprise", "National Specialist")]
    ordered_quotes = tier1_quotes + tier2_quotes + other_quotes

    _rendered_tier = None
    for i, q in enumerate(ordered_quotes):
        o = q.chosen_option

        # ── Tier section header (render once per tier) ──────────────────────
        cur_tier = o.merchant_type
        if cur_tier != _rendered_tier:
            _rendered_tier = cur_tier
            if cur_tier == "Enterprise":
                st.markdown(
                    '<div class="tier-hdr tier-hdr-1">&#9679; Tier 1 — Preferred National Distributors'
                    ' &nbsp;<span style="font-weight:400;color:#8b949e;">(Grainger · McMaster-Carr · MSC)</span></div>',
                    unsafe_allow_html=True)
            elif cur_tier == "National Specialist":
                st.markdown(
                    '<div class="tier-hdr tier-hdr-2">&#9679; Tier 2 — National Specialists</div>',
                    unsafe_allow_html=True)

        _suit_score = getattr(o, "suitability_score", 100.0)
        _suit_ok    = _suit_score >= 60   # gate: must meet minimum relevance threshold
        is_best   = (q == quotes[0]) and _suit_ok
        card_cls  = "q-card q-card-best" if is_best else "q-card"
        _wf       = st.session_state.workflow_mode
        _best_lbl = "Best TLV" if _wf != "spare_parts" else "Best TCA"
        best_html = f'<span class="best-badge">{_best_lbl}</span>' if is_best else ""
        rfq_html  = '<div class="rfq-warning">&#9888; Requires 24-48h for manual outreach &middot; +$50 admin fee</div>' if o.requires_rfq else ""

        # "Preferred" badge: 100% PN exact match across all workflows — differentiates
        # the original from functional equivalents returned by dual-search.
        # Requires suitability ≥ 60 to prevent low-confidence results from claiming Preferred.
        _searched_pn_raw  = (st.session_state.specs.part_number or "") if st.session_state.specs else ""
        _found_pn_raw     = getattr(o, "found_part_number", None) or ""
        _pn_100_match     = (_searched_pn_raw and _found_pn_raw and
                             _found_pn_raw.upper().strip() == _searched_pn_raw.upper().strip())
        _preferred_html   = (
            '<span class="best-badge" style="border-color:#3fb950;color:#3fb950;margin-left:.3rem;">&#10003; Preferred</span>'
            if (_pn_100_match and _suit_ok)
            else ""
        )

        # Price-source badge: Cached / Pre-Negotiated / Live
        notes = o.notes or ""
        if notes.startswith("Pre-Negotiated"):
            src_badge = '<span class="chip chip-g" style="font-size:.62rem;letter-spacing:.04em;">PRE-NEGOTIATED</span>'
        elif notes.startswith("Cached"):
            src_badge = '<span class="chip chip-a" style="font-size:.62rem;letter-spacing:.04em;">CACHED</span>'
        else:
            src_badge = '<span class="chip chip-b" style="font-size:.62rem;letter-spacing:.04em;">LIVE</span>'

        # Collection / search-page warning badge
        coll_badge = (
            '<span class="chip chip-r" style="font-size:.62rem;" '
            'title="URL is a list/search page — not a direct product page">&#9888; LIST PAGE</span>'
            if getattr(o, "is_collection_page", False) else ""
        )

        col_info, col_price, col_btn = st.columns([4, 3, 1.2])
        url_html = (
            f'<div style="margin-top:.3rem;font-size:.7rem;">'
            f'<a href="{o.source_url}" target="_blank" rel="noopener noreferrer" '
            f'style="color:#58a6ff;text-decoration:none;">&#128279; View Source</a></div>'
        ) if o.source_url else ""

        mt = o.merchant_type
        if mt == "Enterprise":
            mt_badge = '<span class="chip chip-g" style="font-size:.62rem;">ENTERPRISE</span>'
        elif mt == "National Specialist":
            mt_badge = '<span class="chip chip-a" style="font-size:.62rem;">NATIONAL SPECIALIST</span>'
        else:
            mt_badge = '<span class="chip" style="font-size:.62rem;">LOCAL</span>'

        # MoR badge for Tier 2/3
        _mor_html = (
            '<span class="mor-badge">Purchasable via Arkim</span>'
            if mt not in ("Enterprise",) else ""
        )

        alt_badge = (
            '<span class="chip chip-a" style="font-size:.62rem;letter-spacing:.03em;">ALT RECOMMENDATION</span>'
            if getattr(o, "match_type", "Exact") == "Alternative" else ""
        )

        # Market confidence score chip + Verified Reliability badge
        _mcs = getattr(o, "market_confidence_score", None)
        _mcs_html = (
            f'<span class="chip" title="Market Confidence Score (web reliability data)" '
            f'style="font-size:.62rem;border-color:#58a6ff;color:#58a6ff;">MCS {_mcs:.0f}/10</span>'
            if _mcs is not None else ""
        )
        _reliability_html = (
            '<span style="color:#3fb950;font-weight:700;">&#10003; Verified Reliability</span>'
            if (_mcs is not None and _mcs > 8)
            else f'Reliability: <b style="color:#c9d1d9;">{o.reliability_score:.0f}%</b>'
        )

        # Warranty chip
        _wt = getattr(o, "warranty_terms", None)
        _wt_html = (
            f'<span class="chip" style="font-size:.62rem;">{_wt}</span>'
            if _wt else ""
        )

        # High Replacement Risk badge (spare parts only, MCS < 5)
        _labor = getattr(q, "labor_impact_cost", 0.0)
        if _labor > 0:
            _risk_html = (
                f'<div style="margin-top:.3rem;background:rgba(248,81,73,.1);border:1px solid '
                f'rgba(248,81,73,.4);border-radius:4px;padding:.18rem .5rem;font-size:.7rem;color:#f85149;">'
                f'&#9888; High Replacement Risk — low market confidence (MCS {_mcs:.0f}/10) '
                f'adds est. <b>${_labor:,.0f}</b> in labor. '
                f'Lower purchase price may be offset by higher TCO.</div>'
            )
        else:
            _risk_html = ""

        # Score display row: TCA for spare parts, TLV for replacement
        _wf_mode = st.session_state.workflow_mode
        if _wf_mode == "spare_parts":
            _score_html = f'TCA: <b style="color:#58a6ff;">{q.tca_score:.1f}/100</b>'
            if _labor > 0:
                _score_html += f' &nbsp;<span style="color:#f85149;font-size:.7rem;">(+${_labor:,.0f} labor est.)</span>'
        else:
            _score_html = f'TLV: <b style="color:#d29922;">${q.tlv_score:,.2f}</b>'

        with col_info:
            st.markdown(f"""
            <div class="{card_cls}">
              <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.35rem;">
                <span style="font-size:1rem;font-weight:700;color:#e6edf3;">{o.vendor_name}</span>
                {mt_badge}{_mor_html}
                {src_badge}{best_html}{_preferred_html}{alt_badge}{coll_badge}
              </div>
              <div style="font-size:.77rem;color:#8b949e;">
                Lead: <b style="color:#c9d1d9;">{o.lead_time_days}d</b> &nbsp;&middot;&nbsp;
                {_reliability_html} &nbsp;&middot;&nbsp;
                {_score_html}
              </div>
              <div style="margin-top:.25rem;">{_mcs_html}{_wt_html}</div>
              {_risk_html}{rfq_html}{url_html}
            </div>""", unsafe_allow_html=True)

        with col_price:
            _is_ltl      = getattr(q, "shipping_ltl", False)
            _ship_label  = getattr(q, "shipping_label", "") or ("LTL Freight Required" if _is_ltl else f"${q.shipping_cost:,.2f}")
            _is_freight_label = _is_ltl or _ship_label in ("LTL Freight Required", "S.F.Q.", "TBA - Freight")

            # Part Number Found row — always visible; red UNVERIFIED when absent
            _found_pn    = getattr(o, "found_part_number", None)
            _searched_pn = (st.session_state.specs.part_number or "") if st.session_state.specs else ""
            _pn_match    = _found_pn and _found_pn.upper().strip() == _searched_pn.upper().strip()
            if _found_pn:
                _pn_color = "#3fb950" if _pn_match else "#d29922"
                _pn_val   = f'<span style="color:{_pn_color};font-weight:600;">{_found_pn}</span>'
                if not _pn_match:
                    _pn_val += ' <span style="color:#8b949e;font-size:.65rem;">(alt)</span>'
            else:
                _pn_val = '<span style="color:#f85149;font-weight:600;">UNVERIFIED</span>'
            _pn_row = (
                f'<tr><td style="color:#8b949e;padding:.04rem 0;">PN Found</td>'
                f'<td style="text-align:right;font-size:.72rem;">{_pn_val}</td></tr>'
            )

            if _is_freight_label:
                _ship_cell  = f'<td style="text-align:right;color:#d29922;font-size:.7rem;">{_ship_label}</td>'
                _total_cell = f'<td style="text-align:right;font-weight:700;color:#3fb950;padding:.12rem 0 0;">${q.grand_total:,.2f} + Freight</td>'
                _total_lbl  = 'Grand Total <span style="font-size:.62rem;color:#8b949e;font-weight:400;">(excl. freight)</span>'
            else:
                _ship_cell  = f'<td style="text-align:right;">{_ship_label}</td>'
                _total_cell = f'<td style="text-align:right;font-weight:700;color:#3fb950;padding:.12rem 0 0;">${q.grand_total:,.2f}</td>'
                _total_lbl  = 'Grand Total'

            st.markdown(f"""
            <div class="{card_cls}">
              <table style="font-size:.77rem;width:100%;border-collapse:collapse;">
                <tr><td style="color:#8b949e;padding:.04rem 0;">Vendor Base</td><td style="text-align:right;">${o.base_price:,.2f}</td></tr>
                <tr><td style="color:#8b949e;padding:.04rem 0;">Shipping</td>{_ship_cell}</tr>
                {_pn_row}
                <tr><td style="color:#8b949e;padding:.04rem 0;">Arkim Price</td><td style="text-align:right;color:#e6edf3;font-weight:600;">${q.arkim_sale_price:,.2f}</td></tr>
                <tr><td style="color:#8b949e;padding:.04rem 0;">Tax</td><td style="text-align:right;">${q.tax_amount:,.2f}</td></tr>
                <tr style="border-top:1px solid #30363d;">
                  <td style="padding:.12rem 0 0;font-weight:700;color:#3fb950;">{_total_lbl}</td>
                  {_total_cell}
                </tr>
              </table>
            </div>""", unsafe_allow_html=True)

        with col_btn:
            st.markdown("<div style='padding-top:.7rem'></div>", unsafe_allow_html=True)
            if st.button(
                "Accept Offer",
                key=f"accept_{q.quote_id}",
                type="primary" if is_best else "secondary",
                use_container_width=True,
            ):
                from datetime import datetime as _dt
                st.session_state.accepted_quote = q
                st.session_state.rfq_campaign_sent = False
                st.session_state.rfq_vendors_selected = []
                st.session_state.active_tab = "🔍 Active Sourcing"
                st.session_state.order_history.append({
                    "Asset":       f"{st.session_state.specs.manufacturer} {st.session_state.specs.model}",
                    "Part No.":    st.session_state.specs.part_number,
                    "Vendor":      o.vendor_name,
                    "Site":        st.session_state.site,
                    "Grand Total": q.grand_total,
                    "Quote ID":    q.quote_id,
                    "Accepted":    _dt.now().strftime("%Y-%m-%d %H:%M"),
                    "Status":      "Processing",
                })
                st.rerun()

    if quotes:
        _wf_note = st.session_state.workflow_mode
        if _wf_note == "spare_parts":
            _metric_note = "TCA = Speed 35% + Reliability 25% + Friction 20% + Cost Efficiency 20%. Preferred badge = 100% PN match."
        else:
            _metric_note = "TLV = Purchase Price + (Downtime Cost × Lead Days × Reliability Risk) + Shipping + Tax. Lower TLV = better lifecycle value."
        st.markdown(f'<div style="font-size:.7rem;color:#8b949e;margin-top:.2rem;">'
                    f'{_metric_note} &nbsp;·&nbsp; Grand Total includes Arkim markup + sales tax. '
                    f'Purchasable via Arkim — no new vendor onboarding required.'
                    f'</div>', unsafe_allow_html=True)


def render_purchase_confirmed() -> None:
    from datetime import datetime as _dt
    q   = st.session_state.accepted_quote
    s   = st.session_state.specs
    opt = q.chosen_option
    cost_basis = opt.base_price + q.admin_fee + q.shipping_cost
    markup_amt = q.arkim_sale_price - cost_basis

    st.markdown("""
    <div class="confirmed-banner">
      <h2>&#10003; Purchase Confirmed</h2>
      <p>Arkim is now processing this order via our Merchant of Record account.</p>
    </div>""", unsafe_allow_html=True)

    c1, c2 = st.columns([3, 1])
    with c1:
        admin_row = (
            f'<tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Admin Fee (RFQ)</td>'
            f'<td style="text-align:right;">${q.admin_fee:,.2f}</td></tr>'
        ) if q.admin_fee else ""
        _conf_ltl    = getattr(q, "shipping_ltl", False)
        _conf_slabel = getattr(q, "shipping_label", "") or ("LTL Freight Required" if _conf_ltl else f"${q.shipping_cost:,.2f}")
        _conf_freight = _conf_ltl or _conf_slabel in ("LTL Freight Required", "S.F.Q.", "TBA - Freight")
        ship_row_confirmed = (
            f'<tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Shipping</td>'
            f'<td style="text-align:right;color:#d29922;">{_conf_slabel}</td></tr>'
            if _conf_freight else
            f'<tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Shipping</td>'
            f'<td style="text-align:right;">{_conf_slabel}</td></tr>'
        )
        _grand_display = f"${q.grand_total:,.2f} + Freight" if _conf_freight else f"${q.grand_total:,.2f}"
        st.markdown(f"""
        <div class="a-card">
          <div class="a-label">Arkim Purchase Order</div>
          <div style="font-size:.8rem;color:#8b949e;margin-bottom:.5rem;">
            Quote ID: <code>{q.quote_id}</code> &nbsp;&middot;&nbsp;
            {q.generated_at.strftime('%Y-%m-%d %H:%M')} &nbsp;&middot;&nbsp;
            <b style="color:#e6edf3;">{opt.vendor_name}</b> ({opt.merchant_type})
          </div>
          <div style="font-size:.85rem;margin-bottom:.6rem;">
            <b>Asset:</b> {s.manufacturer} {s.model}
            <span style="color:#8b949e;">({s.part_number})</span>
          </div>
          <table style="font-size:.82rem;border-collapse:collapse;width:100%;">
            <tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Vendor Base Price</td><td style="text-align:right;">${opt.base_price:,.2f}</td></tr>
            {admin_row}
            {ship_row_confirmed}
            <tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;border-top:1px solid #30363d;">Cost Basis</td><td style="text-align:right;border-top:1px solid #30363d;">${cost_basis:,.2f}</td></tr>
            <tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Arkim Markup ({q.arkim_markup_pct:.0f}%)</td><td style="text-align:right;">${markup_amt:,.2f}</td></tr>
            <tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;border-top:1px solid #30363d;">Arkim Service Price</td><td style="text-align:right;border-top:1px solid #30363d;">${q.arkim_sale_price:,.2f}</td></tr>
            <tr><td style="color:#8b949e;padding:.1rem .5rem .1rem 0;">Sales Tax ({q.tax_rate*100:.2f}%)</td><td style="text-align:right;">${q.tax_amount:,.2f}</td></tr>
          </table>
          <div style="font-size:.78rem;color:#58a6ff;margin-top:.6rem;">
            &#10004; Direct Buy via Arkim — no new vendor onboarding required (AVL bypass)
            &nbsp;&middot;&nbsp; Est. {q.estimated_delivery_days} business days
          </div>
        </div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""
        <div class="a-card" style="text-align:center;">
          <div class="m-label">Grand Total</div>
          <div class="price-xl">{_grand_display}</div>
          <div style="font-size:.68rem;color:#8b949e;margin:.2rem 0 .9rem;">incl. tax &amp; Arkim fee{"  · freight TBD" if _conf_freight else ""}</div>
          <div class="m-label">TCA Score</div>
          <div class="tca-xl">{q.tca_score:.0f}</div>
          <div style="font-size:.68rem;color:#8b949e;margin-top:.15rem;">out of 100</div>
        </div>""", unsafe_allow_html=True)

    st.markdown("<div style='margin-top:.75rem'></div>", unsafe_allow_html=True)
    if st.button("Start New Sourcing Run", use_container_width=True):
        for key in ("accepted_quote", "pipeline_ran", "all_quotes", "best_quote",
                    "specs", "all_options", "rfq_draft", "rfq_campaign_sent", "rfq_vendors_selected"):
            st.session_state[key] = _DEFAULTS.get(key)
        st.rerun()


def render_tier3_outreach() -> None:
    all_options = st.session_state.all_options
    rfq_options = [o for o in all_options if getattr(o, "price_tbd", False)]
    if not rfq_options:
        return

    specs = st.session_state.specs

    priced_national = [o for o in all_options
                       if not getattr(o, "price_tbd", False)
                       and o.merchant_type in ("Enterprise", "National Specialist", "Direct Buy via Arkim")]
    no_buy_now = len(priced_national) == 0
    header_note = (
        '<span style="color:#f85149;font-size:.72rem;font-weight:600;margin-left:.5rem;">'
        '&#9888; No Tier 1/1.5 Buy Now pricing found</span>'
        if no_buy_now else ""
    )

    st.markdown(f'<div class="tier3-hdr">Tier 3: Partner Outreach Network{header_note}</div>',
                unsafe_allow_html=True)
    body = (
        '<b style="color:#e6edf3;">No immediate Buy Now pricing was returned by national distributors.</b> '
        'Partner Outreach is your next step — Arkim contacts these specialists on your behalf '
        'and returns a binding quote within 24–48 h. '
        if no_buy_now else
        'National distributors returned Buy Now pricing above. '
        'These specialists may offer better terms — Arkim contacts them on your behalf. '
    )
    st.markdown(
        f'<div style="font-size:.78rem;color:#8b949e;margin-bottom:.75rem;">'
        f'{body}'
        f'Select vendors to contact. Each outreach includes a Partner Invitation '
        f'and adds a <b style="color:#d29922;">$50 admin fee</b>.</div>',
        unsafe_allow_html=True,
    )

    from utils.sourcing import draft_rfq_email

    # Sort: highest suitability first
    rfq_options_sorted = sorted(rfq_options,
                                 key=lambda o: getattr(o, "suitability_score", 0),
                                 reverse=True)

    any_checked = False
    for o in rfq_options_sorted:
        suit  = getattr(o, "suitability_score", 0.0)
        pstat = getattr(o, "partner_status", "")

        # Suitability colour class
        if suit >= 75:
            suit_cls = "suit-hi"
        elif suit >= 50:
            suit_cls = "suit-mid"
        else:
            suit_cls = "suit-lo"

        # Partner badge HTML
        if pstat == "Gold":
            badge_html = '<span class="badge-gold">&#9733; Gold Partner</span>'
        elif pstat == "Silver":
            badge_html = '<span class="badge-silver">&#9651; Silver Target</span>'
        else:
            badge_html = ""

        alt_badge = (
            '<span class="best-badge" style="border-color:#f85149;color:#f85149;margin-left:.3rem;">'
            'Alt Rec</span>'
            if getattr(o, "match_type", "Exact") == "Alternative" else ""
        )

        found_pn = getattr(o, "found_part_number", None)
        pn_note  = f'<span style="color:#8b949e;font-size:.72rem;"> · PN found: {found_pn}</span>' if found_pn else ""
        url_note = (f'<a href="{o.source_url}" target="_blank" '
                    f'style="color:#58a6ff;font-size:.72rem;margin-left:.4rem;">↗ View</a>'
                    if o.source_url else "")

        card_html = (
            f'<div class="t3-card">'
            f'  <div class="t3-suit-col">'
            f'    <div class="t3-suit-num {suit_cls}">{suit:.0f}%</div>'
            f'    <div class="t3-suit-lbl">Match</div>'
            f'  </div>'
            f'  <div class="t3-body">'
            f'    <div class="t3-name">{o.vendor_name}{badge_html}{alt_badge}</div>'
            f'    <div class="t3-meta">'
            f'      Price: TBD (Inquiry Required) · {o.lead_time_days}d lead · '
            f'      Reliability {o.reliability_score:.0f}%'
            f'      {pn_note}{url_note}'
            f'    </div>'
            f'  </div>'
            f'</div>'
        )
        st.markdown(card_html, unsafe_allow_html=True)

        # "Invite to Partner Network" button for high-suitability non-Gold vendors
        from utils.sourcing import _onboarding_url as _ourl, _VERIFIED_PARTNERS as _vp
        if suit >= 75 and pstat != "Gold":
            _invite_url = _ourl(o.vendor_name, specs) if specs else "#"
            _inv_col1, _inv_col2 = st.columns([3, 1])
            with _inv_col1:
                chk_key = f"rfq_chk_{o.vendor_name}"
                checked = st.checkbox(
                    f"Include {o.vendor_name} in outreach (+$50 admin fee)",
                    key=chk_key,
                    value=True,
                )
            with _inv_col2:
                st.link_button(
                    "Invite to Partner Network →",
                    url=_invite_url,
                    use_container_width=True,
                    help=f"Open onboarding link for {o.vendor_name} ({suit:.0f}% match)",
                )
        else:
            chk_key = f"rfq_chk_{o.vendor_name}"
            checked = st.checkbox(
                f"Include {o.vendor_name} in outreach (+$50 admin fee)",
                key=chk_key,
                value=True,
            )

        if checked:
            any_checked = True
            if o.vendor_name not in st.session_state.rfq_emails and specs:
                st.session_state.rfq_emails[o.vendor_name] = draft_rfq_email(specs, o)
            if o.vendor_name in st.session_state.rfq_emails:
                with st.expander(f"Partner Invitation — {o.vendor_name}", expanded=False):
                    st.code(st.session_state.rfq_emails[o.vendor_name], language="text")
                    st.caption("Copy and send via your email client · reply-to: procurement@arkim.ai")

    if any_checked:
        selected = [o.vendor_name for o in rfq_options_sorted
                    if st.session_state.get(f"rfq_chk_{o.vendor_name}", False)]
        st.markdown("<div style='margin-top:.5rem'></div>", unsafe_allow_html=True)
        if st.button("Initiate Partner Outreach Campaign", type="primary"):
            st.session_state.rfq_campaign_sent   = True
            st.session_state.rfq_vendors_selected = selected
            st.rerun()

    if st.session_state.rfq_campaign_sent and st.session_state.rfq_vendors_selected:
        st.success(
            f"Partner outreach initiated — queued for: "
            f"{', '.join(st.session_state.rfq_vendors_selected)}"
        )


def render_empty() -> None:
    st.markdown("""
    <div style="text-align:center;padding:5rem 2rem;color:#8b949e;">
      <div style="font-size:3.5rem;">&#9881;</div>
      <div style="font-size:1.1rem;font-weight:700;color:#e6edf3;margin:.75rem 0 .4rem;">
        Ready to Source
      </div>
      <div style="max-width:440px;margin:0 auto;font-size:.9rem;line-height:1.6;">
        Upload a nameplate photo in the sidebar, or describe the part you need
        in the Chat Assistant below to start a live sourcing run.
      </div>
      <div style="margin-top:2rem;background:#161b22;border:1px solid #30363d;
                  border-radius:8px;padding:.85rem 1.25rem;display:inline-block;
                  text-align:left;font-size:.82rem;line-height:1.7;">
        <span style="color:#58a6ff;font-weight:700;">Try these prompts:</span><br>
        "Source a Square D 8536SCG3V02 magnetic starter for La Mirada"<br>
        "Find me a GE 5KE49TN2167 3HP induction motor"<br>
        "Get pricing for an ABB M3AA090L 2HP motor for Vista"
      </div>
    </div>
    """, unsafe_allow_html=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sidebar
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with st.sidebar:
    st.markdown("""
    <div class="sb-logo">
      <div class="logo-t">&#9881; Arkim</div>
      <div class="logo-s">Procurement Command Center</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="sb-sec">Client Site</div>', unsafe_allow_html=True)
    site_sel = st.selectbox("Site", ["La Mirada", "Vista"], label_visibility="collapsed")

    st.markdown('<div class="sb-sec">Session</div>', unsafe_allow_html=True)
    if st.button("+ New Sourcing Event", use_container_width=True, type="secondary"):
        _clear_keys = [
            "specs", "all_options", "all_quotes", "best_quote", "pipeline_ran",
            "accepted_quote", "rfq_emails", "rfq_draft", "rfq_campaign_sent",
            "rfq_vendors_selected", "pipeline_error", "pending_run", "pending_image",
            "pending_verification_specs", "pending_verification_site", "force_refresh",
            "inventory_hit", "inventory_location", "messages",
            "search_mode", "workflow_mode",
        ]
        for _ck in _clear_keys:
            st.session_state[_ck] = _DEFAULTS.get(_ck)
        # Clear rfq checkbox keys
        for _ck in list(st.session_state.keys()):
            if _ck.startswith("rfq_chk_"):
                del st.session_state[_ck]
        st.session_state.active_tab = "🔍 Active Sourcing"
        st.rerun()

    st.markdown("<div style='flex:1'></div>", unsafe_allow_html=True)

# ── API keys: Streamlit Secrets → environment variable → empty ────────────────
def _get_secret(key: str) -> str:
    try:
        return st.secrets[key]
    except Exception:
        return os.environ.get(key, "")

ant_key = _get_secret("ANTHROPIC_API_KEY")
tav_key = _get_secret("TAVILY_API_KEY")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Main area
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

st.markdown("""
<div class="cmd-hdr">
  <div>
    <h1>&#9881; Arkim Command Center</h1>
    <p>Real-time industrial procurement &middot; Grainger &middot; McMaster-Carr
       &middot; MSC Industrial &middot; TCA Scoring</p>
  </div>
  <div class="live-badge">&#9679; Live</div>
</div>
""", unsafe_allow_html=True)

# ── Analytics bar ─────────────────────────────────────────────────────────────
_SEED_SAVINGS   = 42_850.0
_SEED_BYPASSES  = 12
_live_orders    = st.session_state.order_history
# Estimate savings as 10 % of Arkim grand total for live session orders
_session_savings   = sum(o.get("Grand Total", 0) * 0.10 for o in _live_orders)
_total_savings     = _SEED_SAVINGS + _session_savings
_total_bypasses    = _SEED_BYPASSES + len(_live_orders)
_savings_delta     = f"+${_session_savings:,.0f} this session" if _session_savings else "+$3,200 vs last month"
_bypasses_delta    = f"+{len(_live_orders)} this session" if _live_orders else "+2 this week"

m1, m2, m3 = st.columns(3)
with m1:
    st.metric("Total Procurement Savings", f"${_total_savings:,.0f}", delta=_savings_delta)
with m2:
    st.metric("AVL Bypasses Completed", str(_total_bypasses), delta=_bypasses_delta)
with m3:
    st.metric("Avg. Lead Time Reduction", "6.2 Days", delta="-0.8 days")

st.markdown("<div style='margin-bottom:.75rem'></div>", unsafe_allow_html=True)

# ── Pipeline dispatch — before tabs so st.status() renders at page level ──────
if st.session_state.pending_image:
    img_data = st.session_state.pending_image
    st.session_state.pending_image = None
    with st.status("Reading nameplate image…", expanded=True) as _img_status:
        st.write("Sending image to Claude Vision…")
        try:
            specs = specs_from_image(img_data["bytes"], img_data["filename"], ant_key)
            st.write(f"Identified: **{specs.manufacturer} {specs.model}** — PN: `{specs.part_number}`")
            _img_status.update(label="Nameplate read successfully", state="complete", expanded=False)
            _img_mode = ("exact"
                         if getattr(specs, "category", "Part") == "Part"
                         and st.session_state.workflow_mode == "spare_parts"
                         else "equivalents")
            st.session_state.pending_run = {
                "specs": specs, "site": img_data["site"],
                "search_mode": _img_mode,
                "workflow": st.session_state.workflow_mode,
            }
        except Exception as e:
            st.error(f"Image processing failed: {e}")
            _img_status.update(label="Image read failed", state="error", expanded=True)
    st.rerun()

if st.session_state.pending_run:
    run_data = st.session_state.pending_run

    # ── Stage 1: Spec verification ─────────────────────────────────────────
    if not run_data.get("verified"):
        missing = _missing_critical_specs(run_data["specs"])
        if missing:
            st.session_state.pending_run = None
            sp = run_data["specs"]
            fields = " · ".join(f"**{f}**" for f in missing)
            q = (
                f"I identified **{sp.manufacturer} {sp.model}**"
                + (f" ({sp.detected_type})" if getattr(sp, "detected_type", None) else "")
                + f", but I'm missing {fields} before I can run an accurate search. "
                f"Could you confirm these details? (e.g. *'460V 3-phase, 5 HP, 56C frame'*) "
                f"Or reply **'search anyway'** to proceed with what I have."
            )
            st.session_state.messages.append({"role": "assistant", "content": q})
            st.session_state.pending_verification_specs = sp
            st.session_state.pending_verification_site  = run_data["site"]
            st.rerun()

    run_data = st.session_state.pending_run
    st.session_state.pending_run = None
    fr           = st.session_state.force_refresh
    st.session_state.force_refresh = False
    search_mode  = run_data.get("search_mode", "exact")
    workflow     = run_data.get("workflow", st.session_state.workflow_mode)
    dtc          = st.session_state.downtime_cost_per_day
    try:
        _execute_pipeline(run_data["specs"], run_data["site"], tav_key, ant_key,
                          force_refresh=fr, search_mode=search_mode,
                          workflow=workflow, downtime_cost_per_day=dtc)
        bq = st.session_state.best_quote
        sp = run_data["specs"]
        if bq:
            metric_str = (f"TCA {bq.tca_score:.0f}/100" if workflow == "spare_parts"
                          else f"TLV ${bq.tlv_score:,.2f}")
            reply = (
                f"Sourced **{sp.manufacturer} {sp.model}** for {run_data['site']}. "
                f"Recommended: **{bq.chosen_option.vendor_name}** at "
                f"**${bq.arkim_sale_price:.2f}** | {bq.chosen_option.lead_time_days}d lead | "
                f"{metric_str}. See Live Sourcing tab for full breakdown."
            )
        else:
            tbd = sum(1 for o in st.session_state.all_options if getattr(o, "price_tbd", False))
            reply = (
                f"Sourced **{sp.manufacturer} {sp.model}** for {run_data['site']} — "
                f"no direct buy pricing returned. "
                f"**{tbd}** specialist(s) queued for Tier 3 Partner Outreach. "
                f"Switch to the Live Sourcing tab to review and initiate outreach."
            )
    except Exception as exc:
        st.session_state.pipeline_error = str(exc)
        reply = f"Pipeline error: {exc}"
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()

# ── Tab navigation (radio styled as tabs — supports programmatic selection) ─────
_TABS = ["📊 Analytics", "🔍 Active Sourcing", "📋 History & Drafts"]
active_tab = st.radio(
    "_tab_nav",
    _TABS,
    index=_TABS.index(st.session_state.active_tab),
    horizontal=True,
    label_visibility="collapsed",
)
st.session_state.active_tab = active_tab

# ── Tab 1 · Analytics ─────────────────────────────────────────────────────────
if active_tab == "📊 Analytics":
    st.markdown('<div class="a-label" style="margin:.6rem 0 .4rem;">Spend vs. Savings — Last 6 Months</div>', unsafe_allow_html=True)
    chart_df = pd.DataFrame({
        "Month":            ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar"],
        "Total Spend ($)":  [18200, 22100, 19800, 24300, 21500, 23900],
        "Arkim Savings ($)":[1820,  3315,  2970,  4104,  3870,  4295],
    }).set_index("Month")
    st.area_chart(chart_df, use_container_width=True, height=270)

    st.markdown("<div style='margin-top:.75rem'></div>", unsafe_allow_html=True)
    ic1, ic2, ic3 = st.columns(3)
    with ic1:
        st.markdown("""
        <div class="a-card">
          <div class="a-label">Top Vendor — Q1</div>
          <div style="font-size:1.1rem;font-weight:700;color:#e6edf3;">Grainger</div>
          <div style="font-size:.78rem;color:#8b949e;margin-top:.2rem;">8 of 12 orders fulfilled</div>
        </div>""", unsafe_allow_html=True)
    with ic2:
        st.markdown("""
        <div class="a-card">
          <div class="a-label">Hours Saved via AVL Bypass</div>
          <div style="font-size:1.1rem;font-weight:700;color:#58a6ff;">74.3 hrs</div>
          <div style="font-size:.78rem;color:#8b949e;margin-top:.2rem;">~6.2h avg per order, 12 bypasses</div>
        </div>""", unsafe_allow_html=True)
    with ic3:
        st.markdown("""
        <div class="a-card">
          <div class="a-label">RFQ Outreach Rate</div>
          <div style="font-size:1.1rem;font-weight:700;color:#d29922;">16.7%</div>
          <div style="font-size:.78rem;color:#8b949e;margin-top:.2rem;">2 of 12 orders required manual outreach</div>
        </div>""", unsafe_allow_html=True)

# ── Tab 2 · Active Sourcing ────────────────────────────────────────────────────
elif active_tab == "🔍 Active Sourcing":

    # ── Workflow selector ─────────────────────────────────────────────────────
    _WF_OPTS = [
        ("spare_parts",  "A — Spare Parts (OpEx)",         "Exact PN · Tier 1 priority · TCA ranked"),
        ("replacement",  "B — Replacement Equipment",      "Functional equivalents · Compatibility check · TLV ranked"),
        ("capex",        "C — New Equipment (CapEx)",       "Spec-driven RFQ · Specialist outreach · Proposal workflow"),
    ]
    _wf_labels = [x[1] for x in _WF_OPTS]
    _wf_keys   = [x[0] for x in _WF_OPTS]
    _wf_subs   = {x[0]: x[2] for x in _WF_OPTS}
    _cur_idx   = _wf_keys.index(st.session_state.workflow_mode)
    _sel_label = st.radio(
        "Workflow", _wf_labels, index=_cur_idx, horizontal=True, label_visibility="collapsed"
    )
    _sel_wf = _wf_keys[_wf_labels.index(_sel_label)]
    if _sel_wf != st.session_state.workflow_mode:
        st.session_state.workflow_mode = _sel_wf
        st.rerun()

    st.markdown(
        f'<div style="font-size:.74rem;color:#8b949e;margin:.2rem 0 .75rem;">'
        f'{_wf_subs[_sel_wf]}</div>',
        unsafe_allow_html=True,
    )

    # ── Section C: CapEx form (replaces upload/chat when capex selected) ──────
    if st.session_state.workflow_mode == "capex":
        from utils.models import AssetSpecs as _AS
        st.markdown('<div class="a-label" style="margin:.4rem 0 .6rem;">New Equipment — Proposal Request</div>', unsafe_allow_html=True)
        _wf_c1, _wf_c2 = st.columns(2)
        with _wf_c1:
            _cx_type  = st.text_input("Equipment Type", placeholder="e.g. Vertical Multi-Stage Centrifugal Pump")
            _cx_mfg   = st.text_input("Preferred Manufacturer (optional)", placeholder="e.g. Grundfos, Goulds, Any")
            _cx_specs = st.text_area("Technical Specifications", height=100,
                                     placeholder="HP, GPM, PSI, Voltage, Phase, Frame Size…")
        with _wf_c2:
            _cx_use   = st.text_input("Use Case / Application", placeholder="e.g. Cooling water recirculation")
            _cx_dc    = st.selectbox("Duty Cycle", ["Continuous", "Intermittent", "Standby", "Unknown"])
            _cx_env   = st.text_input("Environmental Constraints (optional)",
                                      placeholder="e.g. Washdown-rated, -20°C ambient, hazardous area")
            _cx_bud   = st.text_input("Max Budget (optional)", placeholder="e.g. $15,000")
            _cx_dtc   = st.number_input("Estimated Downtime Cost / Day ($)", min_value=0.0,
                                        value=st.session_state.downtime_cost_per_day, step=100.0)

        if st.button("Generate CapEx Proposal Requests", type="primary", use_container_width=True):
            if not _cx_type.strip():
                st.error("Please enter an Equipment Type before submitting.")
            elif not ant_key:
                st.error("Anthropic API key not configured.")
            else:
                desc_parts = [p for p in [_cx_specs.strip(), _cx_env.strip()] if p]
                _cx_specs_obj = _AS(
                    manufacturer=_cx_mfg.strip() or "TBD",
                    model=_cx_type.strip(),
                    part_number="CAPEX-RFQ",
                    voltage="TBD",
                    category="Equipment",
                    description=" | ".join(desc_parts) if desc_parts else _cx_type.strip(),
                    detected_type=_cx_type.strip(),
                    use_case=_cx_use.strip() or None,
                    duty_cycle=_cx_dc,
                    budget_max=_cx_bud.strip() or None,
                )
                st.session_state.downtime_cost_per_day = _cx_dtc
                st.session_state.accepted_quote  = None
                st.session_state.rfq_campaign_sent = False
                st.session_state.messages.append({
                    "role": "user",
                    "content": (
                        f"CapEx proposal request: **{_cx_type.strip()}** | "
                        f"Use Case: {_cx_use or '—'} | Duty: {_cx_dc} | "
                        f"Specs: {_cx_specs.strip() or '—'}"
                    ),
                })
                st.session_state.pending_run = {
                    "specs":   _cx_specs_obj,
                    "site":    site_sel,
                    "verified": True,
                    "search_strategy_confirmed": True,
                    "search_mode": "equivalents",
                    "workflow":    "capex",
                }
                st.rerun()

        st.divider()
        if st.session_state.accepted_quote:
            render_purchase_confirmed()
        elif st.session_state.pipeline_ran:
            render_asset_card()
            render_tier3_outreach()
            if st.session_state.pipeline_error:
                st.error(f"Pipeline error: {st.session_state.pipeline_error}")
        else:
            st.markdown("""
            <div style="text-align:center;padding:2.5rem 2rem;color:#8b949e;">
              <div style="font-size:2.5rem;">📋</div>
              <div style="font-size:.92rem;color:#e6edf3;font-weight:700;margin:.5rem 0 .3rem;">
                CapEx Proposal Workflow
              </div>
              <div style="font-size:.82rem;line-height:1.6;max-width:420px;margin:0 auto;">
                Fill in the form above and click <b>Generate CapEx Proposal Requests</b>. Arkim will
                discover specialist vendors and draft Partner Invitation emails for each, including
                your use case and duty cycle requirements.
              </div>
            </div>""", unsafe_allow_html=True)

    else:
        # ── Sections A & B: Upload + Chat ──────────────────────────────────────
        up_col, chat_col = st.columns([1, 1], gap="medium")

        with up_col:
            # Downtime cost input for TLV (Replacement mode only)
            if st.session_state.workflow_mode == "replacement":
                _dtc_val = st.number_input(
                    "Downtime Cost / Day ($) — for TLV ranking",
                    min_value=0.0,
                    value=st.session_state.downtime_cost_per_day,
                    step=100.0,
                    help="Estimated production downtime cost per day — used in Total Life Cycle Value.",
                )
                if _dtc_val != st.session_state.downtime_cost_per_day:
                    st.session_state.downtime_cost_per_day = _dtc_val

            st.markdown('<div class="sb-sec">Nameplate Upload</div>', unsafe_allow_html=True)
            uploaded = st.file_uploader(
                "Upload nameplate photo",
                type=["png", "jpg", "jpeg"],
                label_visibility="collapsed",
            )
            if uploaded:
                st.image(uploaded, use_column_width=True)
                if st.button("Process Nameplate", use_container_width=True, type="primary"):
                    if not ant_key:
                        st.error("Add your Anthropic API key in Settings (sidebar).")
                    else:
                        st.session_state.accepted_quote = None
                        st.session_state.rfq_campaign_sent = False
                        st.session_state.active_tab = "🔍 Active Sourcing"
                        st.session_state.pending_image = {
                            "bytes": uploaded.getvalue(),
                            "filename": uploaded.name,
                            "site": site_sel,
                        }
                        st.session_state.messages.append({
                            "role": "user",
                            "content": f"Process nameplate image: **{uploaded.name}**",
                        })
                        st.rerun()

        with chat_col:
            st.markdown('<div class="sb-sec">Chat Assistant</div>', unsafe_allow_html=True)
            for m in st.session_state.messages[-6:]:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])

            _placeholder = (
                "e.g. Find a replacement for GE 5KE49TN2167 3HP motor"
                if st.session_state.workflow_mode == "replacement"
                else "e.g. Source a Square D 8536SCG3V02 for La Mirada"
            )
            chat_input = st.text_input(
                "Message",
                placeholder=_placeholder,
                label_visibility="collapsed",
                key="chat_text",
            )
            if st.button("Send", use_container_width=True) and chat_input.strip():
                prompt = chat_input.strip()
                st.session_state.messages.append({"role": "user", "content": prompt})
                st.session_state.active_tab = "🔍 Active Sourcing"
                if not ant_key:
                    st.session_state.messages.append({
                        "role": "assistant",
                        "content": "Please add your Anthropic API key in Settings (sidebar).",
                    })
                else:
                    try:
                        if st.session_state.pending_verification_specs is not None:
                            pv_specs = st.session_state.pending_verification_specs
                            pv_site  = st.session_state.pending_verification_site or site_sel
                            if re.search(r"\bsearch anyway\b|\bproceed\b|\bskip\b", prompt, re.I):
                                st.session_state.pending_verification_specs = None
                                st.session_state.pending_verification_site  = None
                                st.session_state.pending_run = {
                                    "specs": pv_specs, "site": pv_site, "verified": True,
                                    "workflow": st.session_state.workflow_mode,
                                }
                            else:
                                classified = classify_message(
                                    f"Spec clarification for {pv_specs.manufacturer} {pv_specs.model}: {prompt}",
                                    ant_key
                                )
                                updated = _merge_spec_clarification(pv_specs, classified)
                                st.session_state.pending_verification_specs = None
                                st.session_state.pending_verification_site  = None
                                st.session_state.pending_run = {
                                    "specs": updated, "site": pv_site, "verified": True,
                                    "workflow": st.session_state.workflow_mode,
                                }
                        else:
                            classified = classify_message(prompt, ant_key)
                            intent    = classified.get("intent", "GENERAL")
                            req_site  = classified.get("site") or site_sel
                            if intent in ("SOURCING", "SPEC_CLARIFICATION") and (
                                classified.get("manufacturer") or classified.get("model")
                                or classified.get("part_number") or classified.get("gpm")
                                or classified.get("detected_type")
                            ):
                                st.session_state.accepted_quote = None
                                st.session_state.rfq_campaign_sent = False
                                specs = specs_from_classification(classified)
                                # Equipment always runs dual-search (exact + equivalents)
                                default_mode = ("exact"
                                               if getattr(specs, "category", "Part") == "Part"
                                               and st.session_state.workflow_mode == "spare_parts"
                                               else "equivalents")
                                st.session_state.pending_run = {
                                    "specs": specs, "site": req_site,
                                    "workflow": st.session_state.workflow_mode,
                                    "search_mode": default_mode,
                                }
                            else:
                                reply = chat_respond(prompt, ant_key)
                                st.session_state.messages.append({"role": "assistant", "content": reply})
                    except Exception as exc:
                        st.session_state.messages.append({"role": "assistant", "content": f"Error: {exc}"})
                st.rerun()

        st.divider()

        if st.session_state.accepted_quote:
            render_purchase_confirmed()
        elif st.session_state.pipeline_ran:
            render_asset_card()
            _rc1, _rc2 = st.columns([6, 1])
            with _rc2:
                if st.button("🔄 Refresh", help="Bypass price cache and fetch fresh prices from Tavily",
                             use_container_width=True):
                    st.session_state.force_refresh = True
                    st.session_state.accepted_quote = None
                    st.session_state.rfq_campaign_sent = False
                    _refresh_cat = getattr(st.session_state.specs, "category", "Part") if st.session_state.specs else "Part"
                    _refresh_mode = ("exact"
                                    if _refresh_cat == "Part" and st.session_state.workflow_mode == "spare_parts"
                                    else "equivalents")
                    st.session_state.pending_run = {
                        "specs":       st.session_state.specs,
                        "site":        st.session_state.site,
                        "workflow":    st.session_state.workflow_mode,
                        "search_mode": _refresh_mode,
                    }
                    st.rerun()
            render_vendor_cards()
            render_tier3_outreach()
            if st.session_state.pipeline_error:
                st.error(f"Pipeline error: {st.session_state.pipeline_error}")
        else:
            render_empty()

# ── Tab 3 · History & Drafts ───────────────────────────────────────────────────
elif active_tab == "📋 History & Drafts":
    # ── Section A: Sourcing History ──
    st.markdown('<div class="a-label" style="margin:.6rem 0 .5rem;">Previous Sourcing Runs</div>', unsafe_allow_html=True)
    if not st.session_state.sourcing_history:
        st.markdown("""
        <div class="empty-state" style="padding:1.5rem;">
          <div class="es-icon">&#128202;</div>
          <div class="es-body">No sourcing history yet. Run the pipeline for a part and it will appear here automatically.</div>
        </div>""", unsafe_allow_html=True)
    else:
        from datetime import timedelta as _td
        for i, d in enumerate(st.session_state.sourcing_history):
            best    = d["all_quotes"][0] if d["all_quotes"] else None
            d_wf    = d.get("workflow", "spare_parts")
            _wf_tag = {"spare_parts": "OpEx", "replacement": "Replacement", "capex": "CapEx"}.get(d_wf, d_wf)

            # Check if any option in this run has warranty expiring within 60 days
            _hist_expiring = False
            for _ho in d.get("all_options", []):
                _hwt = getattr(_ho, "warranty_terms", None)
                if _hwt:
                    _hm = _parse_warranty_months(_hwt)
                    # Use saved_at date as purchase proxy
                    try:
                        _hdt  = _dt.strptime(d["saved_at"][:10], "%Y-%m-%d")
                        _hexp = _hdt + _td(days=(_hm or 0) * 30.44)
                        if 0 < (_hexp - _dt.now()).days <= 60:
                            _hist_expiring = True
                            break
                    except Exception:
                        pass

            _expiry_badge = (
                ' &nbsp;<span style="background:rgba(210,153,34,.15);border:1px solid '
                'rgba(210,153,34,.5);border-radius:4px;padding:.1rem .35rem;font-size:.64rem;'
                'color:#d29922;">&#9888; Warranty Expiring</span>'
                if _hist_expiring else ""
            )

            dc1, dc2 = st.columns([5, 1])
            with dc1:
                if best:
                    _metric = (f"TCA {best.tca_score:.0f}/100"
                               if d_wf == "spare_parts" else
                               f"TLV ${best.tlv_score:,.2f}")
                    price_txt = f" — best: **{best.chosen_option.vendor_name}** ${best.grand_total:,.2f} | {_metric}"
                else:
                    price_txt = ""
                st.markdown(f"""
                <div class="draft-card">
                  <div style="font-size:.92rem;font-weight:700;color:#e6edf3;">{d['label']}{_expiry_badge}</div>
                  <div style="font-size:.76rem;color:#8b949e;margin-top:.2rem;">
                    Site: {d['site']} &nbsp;&middot;&nbsp;
                    Workflow: <b style="color:#c9d1d9;">{_wf_tag}</b> &nbsp;&middot;&nbsp;
                    Sourced: {d['saved_at']}{price_txt}
                  </div>
                </div>""", unsafe_allow_html=True)
            with dc2:
                st.markdown("<div style='padding-top:.55rem'></div>", unsafe_allow_html=True)
                if st.button("Load", key=f"load_hist_{i}", use_container_width=True, type="primary"):
                    st.session_state.specs                = d["specs"]
                    st.session_state.all_quotes           = d["all_quotes"]
                    st.session_state.all_options          = d["all_options"]
                    st.session_state.rfq_draft            = None
                    st.session_state.rfq_emails           = {}
                    st.session_state.site                 = d["site"]
                    st.session_state.pipeline_ran         = True
                    st.session_state.accepted_quote       = None
                    st.session_state.rfq_campaign_sent    = False
                    st.session_state.active_tab           = "🔍 Active Sourcing"
                    # Restore workflow context
                    st.session_state.workflow_mode        = d.get("workflow", "spare_parts")
                    # Restore suitability/reliability — these live on the options objects
                    # (no separate key needed; objects carry the data)
                    # Pre-check Tier 3 checkboxes for loaded run
                    for _k in list(st.session_state.keys()):
                        if _k.startswith("rfq_chk_"):
                            del st.session_state[_k]
                    for _o in d["all_options"]:
                        if _o.requires_rfq:
                            st.session_state[f"rfq_chk_{_o.vendor_name}"] = True
                    st.rerun()

    st.markdown("<div style='margin-top:1.5rem'></div>", unsafe_allow_html=True)

    # ── Section B: Confirmed Orders ──
    st.markdown('<div class="a-label" style="margin:.6rem 0 .5rem;">Confirmed Orders</div>', unsafe_allow_html=True)
    _seed = [
        {"Asset": "Square D 8536SCG3V02", "Part No.": "8536SCG3V02", "Vendor": "Grainger",
         "Site": "La Mirada", "Grand Total": 1240.50, "Quote ID": "ARK-A1B2C3D4",
         "Accepted": "2026-04-01 09:14", "Status": "Delivered"},
        {"Asset": "GE 5KE49TN2167", "Part No.": "5KE49TN2167", "Vendor": "McMaster-Carr",
         "Site": "Vista", "Grand Total": 890.25, "Quote ID": "ARK-E5F6G7H8",
         "Accepted": "2026-04-08 14:32", "Status": "Delivered"},
        {"Asset": "ABB M3AA090L 2HP", "Part No.": "M3AA090L", "Vendor": "MSC Industrial",
         "Site": "La Mirada", "Grand Total": 2100.00, "Quote ID": "ARK-I9J0K1L2",
         "Accepted": "2026-04-15 11:05", "Status": "In Transit"},
        {"Asset": "Baldor EM3311T", "Part No.": "EM3311T", "Vendor": "Grainger",
         "Site": "Vista", "Grand Total": 650.75, "Quote ID": "ARK-M3N4O5P6",
         "Accepted": "2026-04-19 16:48", "Status": "Delivered"},
        {"Asset": "Siemens 1LE1001", "Part No.": "1LE1001", "Vendor": "McMaster-Carr",
         "Site": "La Mirada", "Grand Total": 1580.00, "Quote ID": "ARK-Q7R8S9T0",
         "Accepted": "2026-04-21 10:22", "Status": "In Transit"},
    ]
    live_orders = st.session_state.order_history
    all_orders  = live_orders + [r for r in _seed if r["Quote ID"] not in {o.get("Quote ID") for o in live_orders}]
    st.dataframe(
        pd.DataFrame(all_orders),
        hide_index=True,
        use_container_width=True,
        column_config={
            "Grand Total": st.column_config.NumberColumn("Grand Total", format="$%.2f"),
            "Status":      st.column_config.TextColumn("Status"),
        },
    )

    st.markdown("<div style='margin-top:1.5rem'></div>", unsafe_allow_html=True)

    # ── Section C: Warranty Registry ──
    st.markdown('<div class="a-label" style="margin:.6rem 0 .5rem;">Warranty Registry</div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:.76rem;color:#8b949e;margin-bottom:.75rem;">'
        'Coverage status derived from order date + extracted warranty period. '
        'Update manually when vendor confirms warranty terms.</div>',
        unsafe_allow_html=True,
    )

    from datetime import datetime as _dt
    import re as _re

    def _parse_warranty_months(terms: str) -> int | None:
        """Extract warranty duration in months from a warranty_terms string."""
        if not terms:
            return None
        t = terms.lower()
        m = _re.search(r"(\d+)\s*-?\s*year", t)
        if m:
            return int(m.group(1)) * 12
        m = _re.search(r"(\d+)\s*-?\s*month", t)
        if m:
            return int(m.group(1))
        return None

    def _warranty_status(accepted_str: str, months: int | None) -> str:
        if months is None:
            return "Unknown"
        try:
            accepted_dt = _dt.strptime(accepted_str[:10], "%Y-%m-%d")
        except Exception:
            return "Unknown"
        from datetime import timedelta
        expiry = accepted_dt + timedelta(days=months * 30.44)
        now    = _dt.now()
        if expiry <= now:
            return "Expired"
        if (expiry - now).days <= 60:
            return "Expiring Soon"
        return "Active"

    # Collect warranty data from accepted orders + options
    _w_rows = []
    # From live session options (have warranty_terms)
    for _h in st.session_state.sourcing_history:
        for _o in _h.get("all_options", []):
            _wt = getattr(_o, "warranty_terms", None)
            if not _wt:
                continue
            # Find matching order
            _match = next(
                (ord_ for ord_ in all_orders
                 if _o.vendor_name == ord_.get("Vendor")),
                None
            )
            if _match:
                _months = _parse_warranty_months(_wt)
                _status = _warranty_status(_match.get("Accepted", ""), _months)
                _w_rows.append({
                    "Asset":          _match.get("Asset", "—"),
                    "Vendor":         _o.vendor_name,
                    "Accepted":       _match.get("Accepted", "—")[:10],
                    "Warranty Terms": _wt,
                    "Coverage":       _status,
                })

    # Seed warranty data for demo orders
    _seed_warranties = [
        {"Asset": "Square D 8536SCG3V02",  "Vendor": "Grainger",      "Accepted": "2026-04-01",
         "Warranty Terms": "12-month standard",  "Coverage": "Active"},
        {"Asset": "GE 5KE49TN2167",         "Vendor": "McMaster-Carr", "Accepted": "2026-04-08",
         "Warranty Terms": "12-month standard",  "Coverage": "Active"},
        {"Asset": "ABB M3AA090L 2HP",       "Vendor": "MSC Industrial","Accepted": "2026-04-15",
         "Warranty Terms": "24-month standard",  "Coverage": "Active"},
        {"Asset": "Baldor EM3311T",          "Vendor": "Grainger",      "Accepted": "2026-04-19",
         "Warranty Terms": "12-month standard",  "Coverage": "Active"},
        {"Asset": "Siemens 1LE1001",         "Vendor": "McMaster-Carr", "Accepted": "2026-04-21",
         "Warranty Terms": "18-month standard",  "Coverage": "Active"},
    ]
    # Merge: live rows take priority
    _live_assets = {r["Asset"] for r in _w_rows}
    _all_warranties = _w_rows + [r for r in _seed_warranties if r["Asset"] not in _live_assets]

    if _all_warranties:
        _wdf = pd.DataFrame(_all_warranties)

        # Proactive expiry alert
        _expiring = [r for r in _all_warranties if r.get("Coverage") == "Expiring Soon"]
        if _expiring:
            _exp_names = ", ".join(r["Asset"] for r in _expiring)
            st.markdown(
                f'<div style="background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.4);'
                f'border-radius:6px;padding:.45rem .75rem;font-size:.78rem;color:#d29922;margin-bottom:.6rem;">'
                f'&#9888; <b>Warranty expiring within 60 days:</b> {_exp_names}</div>',
                unsafe_allow_html=True,
            )

        st.dataframe(
            _wdf,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Coverage": st.column_config.TextColumn("Coverage"),
            },
        )
    else:
        st.markdown("""
        <div class="empty-state" style="padding:1.2rem;">
          <div class="es-icon">&#9989;</div>
          <div class="es-body">No warranty data yet. Warranty terms are automatically extracted
          when vendors list them on their product pages.</div>
        </div>""", unsafe_allow_html=True)
