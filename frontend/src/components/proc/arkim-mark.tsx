import { type CSSProperties } from "react";

/**
 * ArkimMark — the real Arkim logo mark, geometry taken VERBATIM from the source
 * vector (frontend/public/arkim-mark.svg): the top bar + three descending arrow
 * bands. The dark background rect from the source asset is omitted (transparent),
 * and fill is driven by `currentColor` so the same component serves both uses:
 *   - static top brand (inherits the brand text colour)
 *   - the loader, which wraps this and animates a blue cascade over the shapes.
 *
 * Shapes are listed top -> bottom (top bar, upper, middle, bottom band) so a
 * :nth-child cascade in the loader flows downward. Coordinates are unchanged from
 * the source — only document order differs (the shapes do not overlap), so the
 * rendered mark is identical to the source.
 */
export function ArkimMark({
  size = 32,
  className,
  style,
}: {
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      className={className}
      style={style}
      viewBox="0 0 500 500"
      width={size}
      height={size}
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M290.86,124.05h-25.6v17.56h-25.6v-17.56H98.88l36.87,46.84h233.41l36.87-46.84h-115.18Z" />
      <polygon points="316.45 270.07 348.45 238.07 316.45 206.08 188.47 206.08 188.47 206.08 188.47 174.08 156.47 206.08 188.47 238.07 316.45 238.07 316.45 238.07 316.45 270.07" />
      <polygon points="335.65 305.26 303.66 273.27 201.27 273.27 201.27 241.27 169.27 273.27 201.27 305.26 303.66 305.26 303.66 337.26 335.65 305.26" />
      <polygon points="278.06 404.45 310.05 372.45 278.06 340.46 226.87 340.46 226.87 340.46 226.87 308.46 194.87 340.46 226.87 372.45 278.06 372.45 278.06 372.45 278.06 404.45" />
    </svg>
  );
}
