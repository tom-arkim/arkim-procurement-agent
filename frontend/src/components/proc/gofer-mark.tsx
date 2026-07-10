import { type CSSProperties } from "react";
import Image from "next/image";

/**
 * GoferMark — the Gofer brand mark shown in the top-left header next to the
 * "gofer" wordmark.
 *
 * INTERIM: this renders a raster stand-in logo (a white geometric gopher face
 * on an orange rounded square, `public/gofer-mark.webp`, 256px downscaled from a
 * 1024px source). It is a placeholder pending the real vector logo — when that
 * arrives, swap the asset out here and every call site is updated at once (the
 * component is the single seam, so keep it as the wrapper).
 *
 * Explicit width/height (= `size`) reserve the box so there's no layout shift on
 * load. Uses next/image to match the repo convention (and its ESLint rule).
 */
export function GoferMark({
  size = 32,
  className,
  style,
}: {
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <Image
      src="/gofer-mark.webp"
      alt="Gofer"
      width={size}
      height={size}
      className={className}
      style={style}
      priority
    />
  );
}
