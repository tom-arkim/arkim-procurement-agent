import Link from "next/link";

/**
 * Root page — renders the Arkim wordmark with design-system fonts and colors,
 * satisfying the Phase 1 completion criterion before Phase 2 adds real screens.
 */
export default function RootPage() {
  return (
    <main
      className="flex min-h-screen flex-col items-center justify-center gap-8"
      style={{ background: "var(--bg-1)" }}
    >
      {/* Wordmark */}
      <div className="flex flex-col items-center gap-3">
        {/* Logo mark */}
        <div
          className="flex h-10 w-10 items-center justify-center rounded"
          style={{
            background: "linear-gradient(135deg, var(--blue-50), #2563eb)",
            fontFamily: "var(--font-jetbrains)",
            fontWeight: 600,
            fontSize: 20,
            color: "#07101e",
          }}
        >
          A
        </div>

        {/* Display heading — Inter · 32 / 600 */}
        <h1
          style={{
            fontFamily: "var(--font-inter)",
            fontSize: 32,
            fontWeight: 600,
            lineHeight: 1.05,
            letterSpacing: "-0.025em",
            color: "var(--fg-1)",
          }}
        >
          Arkim
        </h1>

        {/* Subtitle — Inter · 13.5 */}
        <p
          style={{
            fontFamily: "var(--font-inter)",
            fontSize: 13.5,
            color: "var(--fg-2)",
            letterSpacing: "-0.005em",
          }}
        >
          Sourcing Engine
        </p>
      </div>

      {/* Mono caption — JetBrains Mono */}
      <p
        style={{
          fontFamily: "var(--font-jetbrains)",
          fontSize: 10.5,
          fontWeight: 500,
          textTransform: "uppercase",
          letterSpacing: "0.10em",
          color: "var(--fg-3)",
        }}
      >
        Phase 1 · Infrastructure
      </p>

      {/* Color swatches — spot-check that design tokens resolved */}
      <div className="flex gap-2">
        {[
          { bg: "var(--blue-50)", label: "Blue" },
          { bg: "var(--green-50)", label: "Green" },
          { bg: "var(--amber-50)", label: "Amber" },
          { bg: "var(--red-50)", label: "Red" },
        ].map(({ bg, label }) => (
          <div
            key={label}
            className="flex flex-col items-center gap-1.5"
          >
            <div
              className="h-5 w-5 rounded-sm"
              style={{ background: bg }}
            />
            <span
              style={{
                fontFamily: "var(--font-jetbrains)",
                fontSize: 9.5,
                color: "var(--fg-4)",
                textTransform: "uppercase",
                letterSpacing: "0.08em",
              }}
            >
              {label}
            </span>
          </div>
        ))}
      </div>

      {/* Nav links */}
      <div className="flex gap-3">
        <Link
          href="/runs"
          style={{
            fontFamily: "var(--font-jetbrains)",
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--blue-fg)",
            border: "1px solid var(--blue-line)",
            background: "var(--blue-tint)",
            borderRadius: 4,
            padding: "6px 12px",
            textDecoration: "none",
          }}
        >
          Sourcing runs →
        </Link>
        <a
          href="/api/health"
          target="_blank"
          rel="noreferrer"
          style={{
            fontFamily: "var(--font-jetbrains)",
            fontSize: 10.5,
            fontWeight: 500,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--fg-3)",
            border: "1px solid var(--hr-3)",
            borderRadius: 4,
            padding: "6px 12px",
            textDecoration: "none",
          }}
        >
          API health ↗
        </a>
      </div>
    </main>
  );
}
