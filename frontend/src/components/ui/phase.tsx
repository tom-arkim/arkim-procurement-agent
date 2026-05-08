import { cn } from "@/lib/utils";
import { PHASE_STEPS, phaseToStep } from "@/types";
import type { Phase, PhaseStep } from "@/types";
import { Check } from "./icons";

interface PhaseBarProps {
  phase: Phase;
  className?: string;
}

export function PhaseBar({ phase, className }: PhaseBarProps) {
  const activeStep = phaseToStep(phase);
  const activeIdx = PHASE_STEPS.indexOf(activeStep);

  return (
    <div className={cn("flex items-center gap-0", className)}>
      {PHASE_STEPS.map((step, idx) => {
        const isDone = idx < activeIdx;
        const isActive = idx === activeIdx;
        const isLast = idx === PHASE_STEPS.length - 1;

        return (
          <div key={step} className="flex items-center gap-0">
            <StepNode step={step} done={isDone} active={isActive} />
            {!isLast && <StepConnector done={isDone} />}
          </div>
        );
      })}
    </div>
  );
}

interface StepNodeProps {
  step: PhaseStep;
  done: boolean;
  active: boolean;
}

function StepNode({ step, done, active }: StepNodeProps) {
  return (
    <div className="flex flex-col items-center gap-1">
      <div
        className={cn(
          "flex h-5 w-5 items-center justify-center rounded-full border text-[9px] font-mono font-semibold transition-colors",
          done && "bg-green-50 border-green-50 text-bg-0",
          active && "bg-blue-50 border-blue-50 text-bg-0",
          !done && !active && "bg-bg-3 border-hr-3 text-fg-4",
        )}
      >
        {done ? (
          <Check size={10} strokeWidth={2.5} />
        ) : (
          <span>{step.charAt(0)}</span>
        )}
      </div>
      <span
        className={cn(
          "font-mono text-[9.5px] uppercase tracking-[0.08em] whitespace-nowrap",
          done && "text-green-fg",
          active && "text-blue-fg",
          !done && !active && "text-fg-4",
        )}
      >
        {step}
      </span>
    </div>
  );
}

function StepConnector({ done }: { done: boolean }) {
  return (
    <div
      className={cn(
        "mx-1 mb-4 h-px w-8 transition-colors",
        done ? "bg-green-50" : "bg-hr-2",
      )}
    />
  );
}
