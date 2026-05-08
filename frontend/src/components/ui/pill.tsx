import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Dot — live-state pulse indicator
// ---------------------------------------------------------------------------

interface DotProps {
  tone?: "blue" | "green" | "amber" | "red" | "ghost";
  pulse?: boolean;
  className?: string;
}

const dotColor: Record<NonNullable<DotProps["tone"]>, string> = {
  blue: "bg-blue-fg",
  green: "bg-green-fg",
  amber: "bg-amber-fg",
  red: "bg-red-fg",
  ghost: "bg-fg-3",
};

export function Dot({ tone = "ghost", pulse = false, className }: DotProps) {
  return (
    <span
      className={cn(
        "inline-block h-1.5 w-1.5 rounded-full shrink-0",
        dotColor[tone],
        pulse && "animate-pulse-dot",
        className,
      )}
    />
  );
}

// ---------------------------------------------------------------------------
// Pill
// ---------------------------------------------------------------------------

const pillVariants = cva(
  "inline-flex items-center gap-1.5 rounded font-mono text-[10.5px] font-medium uppercase tracking-[0.08em] leading-none px-1.5 py-0.5",
  {
    variants: {
      tone: {
        blue: "bg-blue-tint text-blue-fg border border-blue-line",
        green: "bg-green-tint text-green-fg border border-green-line",
        amber: "bg-amber-tint text-amber-fg border border-amber-line",
        red: "bg-red-tint text-red-fg border border-red-line",
        ghost: "bg-bg-3 text-fg-3 border border-hr-2",
      },
      solid: {
        true: "",
        false: "",
      },
    },
    compoundVariants: [
      { tone: "blue", solid: true, class: "bg-blue-50 text-bg-0 border-transparent" },
      { tone: "green", solid: true, class: "bg-green-50 text-bg-0 border-transparent" },
      { tone: "amber", solid: true, class: "bg-amber-50 text-bg-0 border-transparent" },
      { tone: "red", solid: true, class: "bg-red-50 text-bg-0 border-transparent" },
      { tone: "ghost", solid: true, class: "bg-fg-4 text-bg-0 border-transparent" },
    ],
    defaultVariants: {
      tone: "ghost",
      solid: false,
    },
  },
);

export interface PillProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof pillVariants> {
  dot?: boolean;
  pulseDot?: boolean;
}

export function Pill({
  className,
  tone = "ghost",
  solid = false,
  dot,
  pulseDot,
  children,
  ...props
}: PillProps) {
  return (
    <span className={cn(pillVariants({ tone, solid }), className)} {...props}>
      {(dot || pulseDot) && (
        <Dot tone={tone ?? "ghost"} pulse={pulseDot} />
      )}
      {children}
    </span>
  );
}
