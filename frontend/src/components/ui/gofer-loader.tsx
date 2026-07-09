"use client";

import { useId, type CSSProperties } from "react";

type GoferLoaderProps = {
  /** width & height in px. The viewBox scales; verified legible down to 48. Default 200. */
  size?: number;
  /** industrial hard-hat variant (.gofer--hat). Default off. */
  hat?: boolean;
  /** freeze on the idle frame as a static icon (.gofer--static). Default off (animated). */
  static?: boolean;
  className?: string;
  style?: CSSProperties;
  /** accessible label on the <svg>. Defaults to "Loading". */
  "aria-label"?: string;
};

/**
 * GoferLoader — the digging-gopher loading animation (designer handoff, approved
 * asset). Pure SVG + CSS: the gf-* @keyframes and .gofer / .gofer--hat /
 * .gofer--static rules live in globals.css. Every layer shares 3.4s / delay:0 to
 * stay in phase — do not retune individual layers.
 *
 * The clipPath id is generated per-instance (useId) and used for BOTH the
 * <clipPath> id and the clip-path="url(#…)" reference, so multiple loaders on one
 * page don't collide — a duplicate id silently breaks the sink-into-ground clip.
 */
export function GoferLoader({
  size = 200,
  hat = false,
  static: isStatic = false,
  className,
  style,
  "aria-label": ariaLabel = "Loading",
}: GoferLoaderProps) {
  // useId can contain ":" (invalid in a bare url(#…) fragment / CSS selector); strip it.
  const clipId = `gfClip-${useId().replace(/:/g, "")}`;
  const cls = ["gofer", hat && "gofer--hat", isStatic && "gofer--static", className]
    .filter(Boolean)
    .join(" ");

  return (
    <svg
      className={cls}
      viewBox="0 0 200 200"
      width={size}
      height={size}
      style={{ display: "block", overflow: "visible", ...style }}
      role="img"
      aria-label={ariaLabel}
    >
      <defs>
        <clipPath id={clipId}>
          <rect x="-80" y="-80" width="360" height="232" />
        </clipPath>
      </defs>

      {/* hole (behind the gopher) */}
      <ellipse cx="100" cy="150" rx="30" ry="9" fill="#4a3826" />
      <ellipse cx="100" cy="149" rx="30" ry="8.5" fill="#3a2b1c" />

      {/* GOPHER (clipped to above-ground so it vanishes when it sinks) */}
      <g data-clip clipPath={`url(#${clipId})`}>
        <g data-anim className="gf-body">
          <g data-anim className="gf-lean">

            {/* tail */}
            <path d="M132 148 q26 -2 30 -18 q-6 -3 -12 2 q-10 8 -20 12 Z" fill="#7a5e42" />

            {/* feet */}
            <ellipse cx="83" cy="147" rx="13" ry="8" fill="#8a6a4b" />
            <ellipse cx="117" cy="147" rx="13" ry="8" fill="#8a6a4b" />

            {/* torso + belly */}
            <ellipse cx="100" cy="116" rx="41" ry="44" fill="#9c7b5a" />
            <path d="M60 116 q0 -34 40 -38 q-30 10 -30 40 q0 26 14 42 q-24 -14 -24 -44 Z" fill="#8a6a4b" opacity="0.6" />
            <ellipse cx="100" cy="123" rx="27" ry="35" fill="#dcc8ab" />
            <ellipse cx="100" cy="120" rx="19" ry="27" fill="#e9dcc4" />

            {/* HEAD */}
            <g data-anim className="gf-head">
              {/* ears */}
              <ellipse cx="72" cy="42" rx="11" ry="13" fill="#8a6a4b" />
              <ellipse cx="128" cy="42" rx="11" ry="13" fill="#8a6a4b" />
              <ellipse cx="72" cy="43" rx="6" ry="8" fill="#c99b86" />
              <ellipse cx="128" cy="43" rx="6" ry="8" fill="#c99b86" />
              {/* head base */}
              <ellipse cx="100" cy="62" rx="39" ry="37" fill="#9c7b5a" />
              <path d="M61 62 q0 -30 39 -33 q-28 8 -28 33 q0 22 12 36 q-23 -13 -23 -36 Z" fill="#8a6a4b" opacity="0.55" />
              {/* cheeks + muzzle */}
              <ellipse cx="70" cy="74" rx="15" ry="16" fill="#b1906c" />
              <ellipse cx="130" cy="74" rx="15" ry="16" fill="#b1906c" />
              <ellipse cx="100" cy="76" rx="24" ry="21" fill="#e4d4b8" />
              {/* eyes */}
              <ellipse cx="84" cy="57" rx="7" ry="7.5" fill="#33251a" />
              <ellipse cx="116" cy="57" rx="7" ry="7.5" fill="#33251a" />
              <circle cx="86.5" cy="54" r="2.3" fill="#fbf7ee" />
              <circle cx="118.5" cy="54" r="2.3" fill="#fbf7ee" />
              {/* nose (Gofer orange) */}
              <path d="M100 68 q7 0 6 5 q-1 5 -6 6 q-5 -1 -6 -6 q-1 -5 6 -5 Z" fill="#f4581c" />
              {/* teeth */}
              <rect x="95.2" y="79" width="4.6" height="10.5" rx="2" fill="#fbf7ee" />
              <rect x="100.2" y="79" width="4.6" height="10.5" rx="2" fill="#efe6d2" />

              {/* HARD HAT (shown only with .gofer--hat) */}
              <g className="gf-hat">
                <ellipse cx="100" cy="41" rx="37" ry="7.5" fill="#d9440f" />
                <ellipse cx="100" cy="40.2" rx="37" ry="6" fill="#f4581c" />
                <path d="M83 40 q17 11 34 0 q-3 7 -17 8 q-14 -1 -17 -8 Z" fill="#e14e18" />
                <path d="M71 41 q29 -35 58 0 Z" fill="#f4581c" />
                <path d="M100 6.5 q29 4 29 34 l-13 0 q0 -22 -16 -30 Z" fill="#d9440f" opacity="0.55" />
                <path d="M100 6.5 q-20 3 -25 24 q9 -16 25 -18 Z" fill="#ff7a45" opacity="0.85" />
                <path d="M100 7 L100 41" stroke="#c8430e" strokeWidth="2.4" strokeLinecap="round" />
              </g>
            </g>

            {/* ARMS / DIGGING PAWS */}
            <g data-anim className="gf-pawL">
              <path d="M74 104 q-14 4 -16 20 q8 6 18 2 Z" fill="#8a6a4b" />
              <ellipse cx="64" cy="122" rx="11" ry="12" fill="#a8875f" />
              <path d="M56 128 l-2 9 l4 -4 Z" fill="#efe6d2" />
              <path d="M62 130 l-1 10 l4 -5 Z" fill="#efe6d2" />
              <path d="M69 129 l1 10 l3 -6 Z" fill="#efe6d2" />
            </g>
            <g data-anim className="gf-pawR">
              <path d="M126 104 q14 4 16 20 q-8 6 -18 2 Z" fill="#8a6a4b" />
              <ellipse cx="136" cy="122" rx="11" ry="12" fill="#a8875f" />
              <path d="M144 128 l2 9 l-4 -4 Z" fill="#efe6d2" />
              <path d="M138 130 l1 10 l-4 -5 Z" fill="#efe6d2" />
              <path d="M131 129 l-1 10 l-3 -6 Z" fill="#efe6d2" />
            </g>

          </g>
        </g>
      </g>

      {/* dirt mounds framing the hole (in front, not clipped) */}
      <ellipse cx="62" cy="153" rx="31" ry="12" fill="#c2a074" />
      <ellipse cx="62" cy="150" rx="24" ry="8" fill="#d2b488" />
      <ellipse cx="138" cy="153" rx="31" ry="12" fill="#c2a074" />
      <ellipse cx="138" cy="150" rx="24" ry="8" fill="#d2b488" />
      <ellipse cx="100" cy="158" rx="70" ry="10" fill="#b8946a" opacity="0.5" />

      {/* flying dirt clods */}
      <g>
        <circle data-anim className="gf-clod1" cx="98" cy="146" r="4.5" fill="#8a6a3e" />
        <circle data-anim className="gf-clod2" cx="102" cy="146" r="4" fill="#a07845" />
        <circle data-anim className="gf-clod3" cx="100" cy="144" r="3.5" fill="#7a5e35" />
        <circle data-anim className="gf-clod4" cx="104" cy="145" r="3" fill="#b0824c" />
        <ellipse data-anim className="gf-puff" cx="100" cy="150" rx="20" ry="7" fill="#d2b488" />
      </g>
    </svg>
  );
}
