import { cn } from "@/lib/cn";

export const ONBOARDING_STEPS = ["Account", "Organization", "Project", "Connect your app"] as const;
export type OnboardingStep = (typeof ONBOARDING_STEPS)[number];

export function StepIndicator({ current }: { current: OnboardingStep }) {
  const currentIndex = ONBOARDING_STEPS.indexOf(current);
  return (
    <ol className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs" aria-label="Setup progress">
      {ONBOARDING_STEPS.map((step, index) => {
        const state = index < currentIndex ? "done" : index === currentIndex ? "current" : "todo";
        return (
          <li key={step} className="flex items-center gap-2" aria-current={state === "current" ? "step" : undefined}>
            <span
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded-full font-mono text-[10px] font-semibold",
                state === "done" && "bg-status-ok-bg text-status-ok",
                state === "current" && "bg-accent text-accent-foreground",
                state === "todo" && "border border-border text-muted",
              )}
            >
              {state === "done" ? "✓" : index + 1}
            </span>
            <span className={cn(state === "current" ? "font-medium text-foreground" : "text-muted")}>{step}</span>
            {index < ONBOARDING_STEPS.length - 1 ? <span className="mx-1 h-px w-4 bg-border" aria-hidden /> : null}
          </li>
        );
      })}
    </ol>
  );
}
