import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  // Base
  "inline-flex items-center justify-center gap-1.5 rounded font-mono text-cap uppercase tracking-[0.08em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-line disabled:pointer-events-none disabled:opacity-40 select-none",
  {
    variants: {
      variant: {
        primary:
          "bg-blue-50 text-bg-0 hover:bg-blue-60 active:bg-blue-70 border border-transparent",
        secondary:
          "bg-bg-3 text-fg-2 border border-hr-3 hover:bg-bg-4 hover:text-fg-1 hover:border-hr-4",
        ghost:
          "bg-transparent text-fg-3 border border-transparent hover:bg-bg-3 hover:text-fg-1",
        destructive:
          "bg-red-tint text-red-fg border border-red-line hover:bg-red-50 hover:text-bg-0 hover:border-transparent",
        success:
          "bg-green-tint text-green-fg border border-green-line hover:bg-green-50 hover:text-bg-0 hover:border-transparent",
        warning:
          "bg-amber-tint text-amber-fg border border-amber-line hover:bg-amber-50 hover:text-bg-0 hover:border-transparent",
        outreach:
          "outreach-btn text-blue-fg border border-blue-line hover:bg-blue-tint",
      },
      size: {
        sm: "h-6 px-2.5 text-[10px]",
        md: "h-8 px-3 text-[10.5px]",
        lg: "h-10 px-4 text-[11px]",
      },
    },
    defaultVariants: {
      variant: "secondary",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  leadingIcon?: React.ReactNode;
  trailingIcon?: React.ReactNode;
  loading?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    { className, variant, size, leadingIcon, trailingIcon, loading, children, disabled, ...props },
    ref,
  ) => {
    return (
      <button
        ref={ref}
        className={cn(buttonVariants({ variant, size }), className)}
        disabled={disabled ?? loading}
        {...props}
      >
        {loading ? (
          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
        ) : (
          leadingIcon && <span className="shrink-0">{leadingIcon}</span>
        )}
        {children}
        {!loading && trailingIcon && <span className="shrink-0">{trailingIcon}</span>}
      </button>
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
