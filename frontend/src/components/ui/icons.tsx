import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function icon(path: React.ReactNode, viewBox = "0 0 16 16") {
  return function Icon({ size = 16, width, height, ...props }: IconProps) {
    return (
      <svg
        viewBox={viewBox}
        width={width ?? size}
        height={height ?? size}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        {...props}
      >
        {path}
      </svg>
    );
  };
}

export const ArrowRight = icon(<path d="M3 8h10M9 4l4 4-4 4" />);
export const ArrowLeft = icon(<path d="M13 8H3M7 4L3 8l4 4" />);
export const ArrowUp = icon(<path d="M8 13V3M4 7l4-4 4 4" />);
export const ArrowDown = icon(<path d="M8 3v10M12 9l-4 4-4-4" />);

export const Check = icon(<path d="M3 8l3.5 3.5L13 4" />);
export const CheckCircle = icon(
  <>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M5.5 8l2 2 3-3" />
  </>
);

export const X = icon(<path d="M4 4l8 8M12 4l-8 8" />);
export const XCircle = icon(
  <>
    <circle cx="8" cy="8" r="6.25" />
    <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />
  </>
);

export const ChevronDown = icon(<path d="M4 6l4 4 4-4" />);
export const ChevronRight = icon(<path d="M6 4l4 4-4 4" />);
export const ChevronLeft = icon(<path d="M10 4L6 8l4 4" />);
export const ChevronUp = icon(<path d="M4 10l4-4 4 4" />);

export const Search = icon(
  <>
    <circle cx="7" cy="7" r="4.25" />
    <path d="M10.5 10.5l2.5 2.5" />
  </>
);

export const Filter = icon(
  <path d="M2 4h12M5 8h6M7.5 12h1" />
);

export const Send = icon(
  <path d="M13.5 2.5L7 9M13.5 2.5L9 14 7 9l-4.5-2 11-4.5z" />
);

export const Copy = icon(
  <>
    <rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1" />
    <path d="M10 5.5V4a1 1 0 00-1-1H4a1 1 0 00-1 1v5a1 1 0 001 1h1.5" />
  </>
);

export const History = icon(
  <>
    <path d="M2.5 8a5.5 5.5 0 105.5-5.5c-1.75 0-3.3.8-4.35 2.05" />
    <path d="M2.5 3.5V6H5" />
    <path d="M8 5.5V8l2 1.5" />
  </>
);

export const Edit = icon(
  <>
    <path d="M10.5 3.5l2 2-6.5 6.5H4v-2l6.5-6.5z" />
    <path d="M9.5 4.5l2 2" />
  </>
);

export const External = icon(
  <>
    <path d="M9 3h4v4" />
    <path d="M13 3L7.5 8.5" />
    <path d="M6 4H4a1 1 0 00-1 1v7a1 1 0 001 1h7a1 1 0 001-1v-2" />
  </>
);

export const Warn = icon(
  <>
    <path d="M8 2.5L1.5 13.5h13L8 2.5z" />
    <path d="M8 6.5v3" />
    <circle cx="8" cy="11.5" r="0.75" fill="currentColor" stroke="none" />
  </>
);

export const Stack = icon(
  <>
    <path d="M2 5.5l6-3 6 3-6 3-6-3z" />
    <path d="M2 8.5l6 3 6-3" />
    <path d="M2 11.5l6 3 6-3" />
  </>
);

export const Pin = icon(
  <>
    <path d="M8 2L9.5 7.5H13L9 10l1 4-2-2.5L6 14l1-4-4-2.5h3.5L8 2z" />
  </>
);

export const Plus = icon(<path d="M8 3v10M3 8h10" />);
export const Minus = icon(<path d="M3 8h10" />);

export const Package = icon(
  <>
    <path d="M2 5.5l6-3 6 3v5l-6 3-6-3v-5z" />
    <path d="M8 2.5v8" />
    <path d="M2 5.5l6 3 6-3" />
    <path d="M5 4l6 3" />
  </>
);

export const Building = icon(
  <>
    <rect x="2.5" y="3.5" width="11" height="9.5" rx="0.5" />
    <path d="M5.5 13V9h5v4" />
    <rect x="5.5" y="5.5" width="2" height="2" />
    <rect x="8.5" y="5.5" width="2" height="2" />
  </>
);

export const Settings = icon(
  <>
    <circle cx="8" cy="8" r="2.5" />
    <path d="M8 2v1.5M8 12.5V14M2 8h1.5M12.5 8H14M3.75 3.75l1.06 1.06M11.19 11.19l1.06 1.06M3.75 12.25l1.06-1.06M11.19 4.81l1.06-1.06" />
  </>
);

export const User = icon(
  <>
    <circle cx="8" cy="5.5" r="2.5" />
    <path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" />
  </>
);

export const Spinner = icon(
  <path d="M8 2a6 6 0 100 12A6 6 0 008 2" strokeDasharray="25" strokeDashoffset="10" />,
);

export const List = icon(
  <path d="M5 4h8M5 8h8M5 12h8M3 4h.01M3 8h.01M3 12h.01" />
);

export const Tag = icon(
  <>
    <path d="M3 3h4.5l5.5 5.5a1 1 0 010 1.4l-3.1 3.1a1 1 0 01-1.4 0L3 7.5V3z" />
    <circle cx="5.5" cy="5.5" r="0.75" fill="currentColor" stroke="none" />
  </>
);

export const Dollar = icon(
  <>
    <path d="M8 2v12" />
    <path d="M10.5 4.5H6.75A2.25 2.25 0 004.5 6.75v0a2.25 2.25 0 002.25 2.25h2.5A2.25 2.25 0 0111.5 11.25v0a2.25 2.25 0 01-2.25 2.25H5" />
  </>
);

export const Clock = icon(
  <>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M8 5v3.5l2.5 1.5" />
  </>
);

export const Truck = icon(
  <>
    <path d="M2 10V5.5a1 1 0 011-1h7v5.5" />
    <path d="M10 5.5h2.5l1.5 2.5V10H10V5.5z" />
    <circle cx="4.5" cy="11" r="1" />
    <circle cx="11.5" cy="11" r="1" />
  </>
);

export const Network = icon(
  <>
    <circle cx="8" cy="8" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="3.5" cy="4" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="12.5" cy="4" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="3.5" cy="12" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="12.5" cy="12" r="1.25" fill="currentColor" stroke="none" />
    <path d="M8 8L3.5 4M8 8l4.5-4M8 8l-4.5 4M8 8l4.5 4" />
  </>
);

export const Globe = icon(
  <>
    <circle cx="8" cy="8" r="5.5" />
    <path d="M8 2.5C6.5 4.5 5.5 6.3 5.5 8s1 3.5 2.5 5.5" />
    <path d="M8 2.5C9.5 4.5 10.5 6.3 10.5 8s-1 3.5-2.5 5.5" />
    <path d="M2.5 8h11" />
    <path d="M3 5.5h10M3 10.5h10" />
  </>
);
