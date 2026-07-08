"""
utils/procurement_agent/onboarding/dom.py

Stdlib-only HTML → pruned-text + link/meta extractor for the onboarding harvester.

No bs4/lxml in this repo (and adding a dep is out of scope for the overnight
build), so this builds a minimal DOM-pruned view from ``html.parser.HTMLParser``:

  - ``PageContent``: the structured, DOM-pruned per-page view the extractor
    consumes — title, meta description/keywords, pruned visible text, anchor
    links (text + abs href), image alt texts (a high-signal brand source on
    distributor line-card pages), and headings.
  - DOM pruning: drops ``<script>``, ``<style>``, ``<noscript>``, ``<template>``,
    ``<svg>``, ``<iframe>``, ``<math>``, ``<canvas>``, ``<object>``, ``<embed>``
    subtrees and ``hidden`` / ``aria-hidden`` / ``display:none`` / ``visibility:hidden``
    elements. Whitespace is collapsed. The result is the bounded, noise-stripped
    text the LLM extractor scores — not raw HTML.

Pure stdlib, no network, no I/O. Deterministic. Fail-soft: malformed HTML
degrades to whatever was parsed so far (HTMLParser is forgiving by design).

The open-element stack is managed with a pop-to-match recovery so mismatched /
unclosed tags (rampant in real marketing HTML, e.g. unclosed ``<p>`` / ``<li>``)
do NOT desynchronize the dropped-subtree or hidden tracking — the lesson from
the first cut, where an early ``aria-hidden`` mobile menu toggle bled ``_hidden``
onto the whole body and collapsed 256KB of brand-page HTML to 216 chars of nav.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse


# Elements whose entire subtree is dropped from the pruned text.
_DROP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "iframe",
    "math", "canvas", "object", "embed", "form", "select", "option",
})

# Void elements (no end tag, no children) — never pushed to the open stack.
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

# Block-ish tags that force a whitespace boundary in the text stream.
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "nav", "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5",
    "h6", "br", "hr", "tr", "td", "th", "table", "tbody", "thead", "tfoot",
    "figure", "figcaption", "blockquote", "pre", "address", "details",
    "summary", "fieldset", "legend",
})

_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4"})


@dataclass
class PageContent:
    """DOM-pruned per-page view.

    ``text`` is the visible, pruned, whitespace-collapsed text (scripts/styles/
    hidden dropped). ``links`` are anchors as (text, absolute-href). ``alt_texts``
    are image alt strings (brand-logo alts are a strong brand signal on line-card
    pages). ``headings`` are h1-h4 texts. All lists dedup-preserve-order.
    """
    url: str
    title: Optional[str] = None
    meta_description: Optional[str] = None
    meta_keywords: Optional[str] = None
    og_title: Optional[str] = None
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)
    alt_texts: list[str] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)

    def text_for_extraction(self, max_chars: int = 16000) -> str:
        """Bounded text blob for the LLM extractor (title + meta + pruned body).

        Bounded so a single giant page can't blow the context budget; the
        extractor sees the head fields + the first ``max_chars`` of pruned body.
        """
        parts: list[str] = []
        if self.title:
            parts.append(f"TITLE: {self.title}")
        if self.og_title and self.og_title != self.title:
            parts.append(f"OG_TITLE: {self.og_title}")
        if self.meta_description:
            parts.append(f"DESCRIPTION: {self.meta_description}")
        if self.meta_keywords:
            parts.append(f"KEYWORDS: {self.meta_keywords}")
        if self.headings:
            parts.append("HEADINGS: " + " | ".join(self.headings[:40]))
        if self.alt_texts:
            # Alt texts (esp. logo alts) are a strong brand signal — include them.
            parts.append("IMAGE_ALTS: " + " | ".join(self.alt_texts[:120]))
        body = self.text or ""
        if len(body) > max_chars:
            body = body[:max_chars] + " …[truncated]"
        parts.append(f"BODY:\n{body}")
        return "\n".join(parts)


class _DOMPruner(HTMLParser):
    """Streaming HTMLParser that emits pruned text + collects links/alts/headings.

    Maintains a single open-element stack of ``(tag, is_dropped, is_locally_hidden)``
    with pop-to-match recovery, so mismatched/unclosed tags (ubiquitous in real
    marketing HTML) cannot desynchronize the dropped-subtree or hidden tracking.
    Forgiving — never raises on malformed input.
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._stack: list[tuple[str, bool, bool]] = []
        # current <title>
        self._in_title = False
        self._title_buf: list[str] = []
        # meta / collected
        self._meta: dict[str, str] = {}
        self._links: list[tuple[str, str]] = []
        self._alts: list[str] = []
        self._headings: list[str] = []
        # current heading
        self._cur_heading_tag: Optional[str] = None
        self._cur_heading_buf: list[str] = []
        # current anchor
        self._cur_link_href: Optional[str] = None
        self._cur_link_buf: list[str] = []
        # body text
        self._text_buf: list[str] = []

    # -- stack helpers ------------------------------------------------------
    @property
    def _in_drop(self) -> bool:
        return any(s[1] for s in self._stack)

    @property
    def _hidden(self) -> bool:
        return any(s[2] for s in self._stack)

    def _attrs_dict(self, attrs: list[tuple[str, Optional[str]]]) -> dict[str, str]:
        return {k: (v or "") for k, v in attrs}

    def _is_hidden(self, attrs: dict[str, str]) -> bool:
        if "hidden" in attrs and attrs.get("hidden", "").lower() not in ("", "false"):
            return True
        style = (attrs.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        aria = (attrs.get("aria-hidden") or "").lower()
        if aria == "true":
            return True
        return False

    # -- parser callbacks ---------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        a = self._attrs_dict(attrs)

        # Void elements: handle their payload, never push.
        if tag in _VOID_TAGS:
            if self._in_drop or self._hidden:
                return
            if tag == "img":
                alt = (a.get("alt") or "").strip()
                if alt:
                    self._alts.append(alt)
            elif tag == "meta":
                name = (a.get("name") or a.get("property") or "").lower()
                content = a.get("content")
                if name and content:
                    self._meta.setdefault(name, content)
            elif tag == "br":
                self._text_buf.append(" ")
            return

        # Dropped subtree: push with is_dropped=True (children inherit it).
        dropped = tag in _DROP_TAGS
        locally_hidden = (not dropped) and self._is_hidden(a)
        self._stack.append((tag, dropped, locally_hidden))
        if dropped or locally_hidden or self._in_drop or self._hidden:
            # Still need to track <a>/<heading> opens inside dropped/hidden so
            # their close tags match the stack, but don't collect their content.
            if tag == "a":
                self._cur_link_href = None  # don't start a link inside dropped
            return

        if tag == "title":
            self._in_title = True
            return
        if tag == "a":
            href = a.get("href")
            self._cur_link_href = href if href else None
            self._cur_link_buf = []
            return
        if tag in _HEADING_TAGS:
            self._cur_heading_tag = tag
            self._cur_heading_buf = []
            return
        if tag in _BLOCK_TAGS:
            self._text_buf.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        # Self-closed form — treat as start (void handling covers img/meta/br).
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        # Pop-to-match: remove entries until we remove one matching `tag` (or the
        # stack empties). This recovers from unclosed/mismatched tags without
        # desyncing the dropped/hidden flags — the robustness fix.
        was_collecting_link = (tag == "a" and self._cur_link_href is not None)
        was_collecting_heading = (tag in _HEADING_TAGS and self._cur_heading_tag == tag)
        idx = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                idx = i
                break
        if idx is not None:
            del self._stack[idx:]
        else:
            # No matching open — ignore the stray close (don't pop unrelated frames).
            return

        if self._in_drop or self._hidden:
            # We were inside a dropped/hidden subtree; just stop any local collection.
            if tag == "a":
                self._cur_link_href = None
                self._cur_link_buf = []
            if tag in _HEADING_TAGS and self._cur_heading_tag == tag:
                self._cur_heading_tag = None
                self._cur_heading_buf = []
            return

        if tag == "title":
            self._in_title = False
            t = "".join(self._title_buf).strip()
            if t:
                self._meta.setdefault("title", t)
            self._title_buf = []
            return
        if was_collecting_link:
            text = "".join(self._cur_link_buf).strip()
            href = self._cur_link_href
            if href:
                abs_href = urljoin(self.base_url, href.strip())
                try:
                    scheme = urlparse(abs_href).scheme.lower()
                except Exception:
                    scheme = ""
                if scheme in ("http", "https"):
                    self._links.append((text, abs_href))
            self._cur_link_href = None
            self._cur_link_buf = []
            self._text_buf.append(" ")
            return
        if was_collecting_heading:
            h = "".join(self._cur_heading_buf).strip()
            if h:
                self._headings.append(h)
            self._cur_heading_tag = None
            self._cur_heading_buf = []
            self._text_buf.append(" ")
            return
        if tag in _BLOCK_TAGS:
            self._text_buf.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_drop or self._hidden:
            return
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._cur_heading_tag is not None:
            self._cur_heading_buf.append(data)
        if self._cur_link_href is not None:
            self._cur_link_buf.append(data)
        self._text_buf.append(data)

    # -- result -------------------------------------------------------------
    def page(self, url: str) -> PageContent:
        raw_text = "".join(self._text_buf)
        text = _collapse_ws(raw_text)
        links = _dedup_pairs(self._links)
        alts = _dedup(self._alts)
        headings = _dedup(self._headings)
        title = self._meta.get("title") or None
        og_title = self._meta.get("og:title") or None
        desc = self._meta.get("description") or None
        kw = self._meta.get("keywords") or None
        return PageContent(
            url=url, title=title, meta_description=desc, meta_keywords=kw,
            og_title=og_title, text=text, links=links, alt_texts=alts,
            headings=headings,
        )


_WS_RE = re.compile(r"\s+")


def _collapse_ws(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        k = it.strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it.strip())
    return out


def _dedup_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for text, href in pairs:
        key = (text.strip().lower(), href.rstrip("/").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append((text.strip(), href))
    return out


def parse_html(html: str, base_url: str) -> PageContent:
    """Parse an HTML document into a DOM-pruned ``PageContent``.

    Fail-soft: malformed HTML → whatever was parsed. Never raises.
    """
    parser = _DOMPruner(base_url=base_url)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # HTMLParser is forgiving; a partial parse is still useful. Don't raise.
        pass
    return parser.page(base_url)
