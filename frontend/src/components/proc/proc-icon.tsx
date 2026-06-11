/**
 * ProcIcon — the customer "Parts & Orders" icon set, paths ported verbatim from the
 * Figma mockup (icons.jsx) so the surface matches precisely. The internal app keeps
 * its own components/ui/icons set; this is scoped to the proc surface.
 */

const PROC_PATHS: Record<string, string> = {
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM21 21l-4.3-4.3",
  bell: "M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0",
  plus: "M12 5v14M5 12h14",
  chevR: "M9 6l6 6-6 6",
  chevD: "M6 9l6 6 6-6",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM12 7v5l3 2",
  checkCircle: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM8.5 12l2.4 2.4L16 9.5",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10ZM12 1v3M12 20v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M1 12h3M20 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z",
  arrowR: "M5 12h14M13 6l6 6-6 6",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM5 21a7 7 0 0 1 14 0",
  alert: "M12 9v4M12 17h.01M10.3 3.9 2.4 18a1.5 1.5 0 0 0 1.3 2.2h16.6a1.5 1.5 0 0 0 1.3-2.2L13.7 3.9a1.5 1.5 0 0 0-2.6 0Z",
  refresh: "M21 12a9 9 0 1 1-3-6.7M21 4v4h-4",
  toolbox: "M4 9a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V9ZM9 8V6.5A2.5 2.5 0 0 1 14 6.5V8M3 13h6M15 13h6M9 11.5h6v3H9z",
  doc: "M14 3v5h5M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5ZM9 13h6M9 17h6",
  sort: "M7 4v16M7 20l-3-3M7 4l3 3M17 4v16M17 4l3 3M17 20l-3-3",
  box: "M3 8l9-5 9 5v8l-9 5-9-5V8ZM3 8l9 5 9-5M12 13v8M16.5 5.5l-9 5",
  truck: "M2 6h12v10H2V6ZM14 9h4l3 3.5V16h-7V9ZM7.5 18a1.8 1.8 0 1 1-3.6 0 1.8 1.8 0 0 1 3.6 0ZM19.5 18a1.8 1.8 0 1 1-3.6 0 1.8 1.8 0 0 1 3.6 0Z",
  tag: "M3 11V4a1 1 0 0 1 1-1h7l10 10-8 8L3 11ZM8.2 8.2h.01",
  mail: "M3 6h18v12H3V6ZM3 7l9 6 9-6",
  spark: "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z",
  building: "M4 21V5a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v16M15 9h4a1 1 0 0 1 1 1v11M4 21h17M8 8h3M8 12h3M8 16h3",
  receipt: "M4 4h16v16l-2-1.5L16 20l-2-1.5L12 20l-2-1.5L8 20l-2-1.5L4 20V4ZM8 9h8M8 13h5",
};

export type ProcIconName = keyof typeof PROC_PATHS;

export function ProcIcon({
  name,
  size = 18,
  stroke = 1.6,
  color = "currentColor",
  className,
}: {
  name: ProcIconName;
  size?: number;
  stroke?: number;
  color?: string;
  className?: string;
}) {
  const d = PROC_PATHS[name];
  if (!d) return null;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      style={{ display: "block", flex: "none" }}
    >
      {d
        .split("M")
        .filter(Boolean)
        .map((seg, i) => (
          <path key={i} d={"M" + seg} />
        ))}
    </svg>
  );
}
