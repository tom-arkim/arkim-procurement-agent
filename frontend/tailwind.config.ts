import type { Config } from "tailwindcss";
import { fontFamily } from "tailwindcss/defaultTheme";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    // -----------------------------------------------------------------------
    // Override (not extend) colors so nothing from the default palette bleeds
    // into the design system. Everything below maps to CSS variables defined
    // in globals.css so the tokens stay a single source of truth.
    // -----------------------------------------------------------------------
    colors: {
      transparent: "transparent",
      current: "currentColor",
      white: "#ffffff",
      black: "#000000",

      // Surface scale
      bg: {
        0: "var(--bg-0)",
        1: "var(--bg-1)",
        2: "var(--bg-2)",
        3: "var(--bg-3)",
        4: "var(--bg-4)",
      },

      // Hairlines
      hr: {
        1: "var(--hr-1)",
        2: "var(--hr-2)",
        3: "var(--hr-3)",
        4: "var(--hr-4)",
      },

      // Text
      fg: {
        1: "var(--fg-1)",
        2: "var(--fg-2)",
        3: "var(--fg-3)",
        4: "var(--fg-4)",
      },

      // Action — Blue
      blue: {
        fg: "var(--blue-fg)",
        50: "var(--blue-50)",
        60: "var(--blue-60)",
        70: "var(--blue-70)",
        tint: "var(--blue-tint)",
        line: "var(--blue-line)",
      },

      // State — Green
      green: {
        fg: "var(--green-fg)",
        50: "var(--green-50)",
        tint: "var(--green-tint)",
        line: "var(--green-line)",
      },

      // State — Amber
      amber: {
        fg: "var(--amber-fg)",
        50: "var(--amber-50)",
        tint: "var(--amber-tint)",
        line: "var(--amber-line)",
      },

      // State — Red
      red: {
        fg: "var(--red-fg)",
        50: "var(--red-50)",
        tint: "var(--red-tint)",
        line: "var(--red-line)",
      },
    },

    extend: {
      fontFamily: {
        sans: ["var(--font-inter)", ...fontFamily.sans],
        mono: ["var(--font-jetbrains)", ...fontFamily.mono],
      },

      fontSize: {
        // Sans type scale
        display: ["32px", { lineHeight: "1.05", fontWeight: "600", letterSpacing: "-0.025em" }],
        h1: ["22px", { lineHeight: "1.25", fontWeight: "600", letterSpacing: "-0.014em" }],
        h2: ["16px", { lineHeight: "1.3", fontWeight: "600", letterSpacing: "-0.008em" }],
        body: ["13.5px", { lineHeight: "1.5" }],
        sm: ["12.5px", { lineHeight: "1.45" }],
        cap: ["10.5px", { lineHeight: "1.2", fontWeight: "500", letterSpacing: "0.10em" }],
        // Mono scale
        "num-lg": ["28px", { lineHeight: "1", fontWeight: "500", letterSpacing: "-0.02em" }],
        "num-md": ["18px", { lineHeight: "1" }],
        "num-sm": ["13px", { lineHeight: "1" }],
      },

      spacing: {
        // Design system spacing ramp
        1: "4px",
        2: "8px",
        3: "12px",
        4: "16px",
        5: "24px",
        6: "32px",
        7: "48px",
        8: "64px",
      },

      borderRadius: {
        none: "0",
        hairline: "2px",
        DEFAULT: "4px",   // buttons, badges
        card: "6px",      // cards
        lg: "10px",       // large surfaces
        full: "9999px",
      },

      boxShadow: {
        card: "0 1px 3px rgba(0,0,0,0.18), 0 4px 16px rgba(0,0,0,0.12)",
        elevated: "0 4px 24px rgba(0,0,0,0.28), 0 1px 4px rgba(0,0,0,0.16)",
        modal: "0 20px 60px rgba(0,0,0,0.35)",
        focus: "0 0 0 3px var(--blue-tint)",
      },

      keyframes: {
        pulse: {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.4" },
        },
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        "fade-out": {
          from: { opacity: "1" },
          to: { opacity: "0" },
        },
        "slide-in-right": {
          from: { transform: "translateX(100%)" },
          to: { transform: "translateX(0)" },
        },
      },

      animation: {
        pulse: "pulse 1.4s ease-in-out infinite",
        "fade-in": "fade-in 0.15s ease-out",
        "fade-out": "fade-out 0.15s ease-in",
        "slide-in": "slide-in-right 0.2s ease-out",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};

export default config;
