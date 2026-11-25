import { FlaskConical, Radio } from "lucide-react";
import type { ServiceModes } from "@/lib/api";

/**
 * Tells the user whether results are real or demo data.
 * Provider keys are configured on the server only; the browser never handles them.
 */
export default function DataModeBanner({
  modes,
  context,
}: {
  modes?: ServiceModes | null;
  /** "preview" = what the next plan will use; "result" = what this plan used */
  context: "preview" | "result";
}) {
  if (!modes) return null;
  const anyMock = Object.values(modes).some((m) => m === "mock");

  if (!anyMock) {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-emerald-400/20 bg-emerald-500/[0.06] px-4 py-2.5 text-sm text-emerald-100/90 animate-rise">
        <Radio className="h-4 w-4 shrink-0 text-emerald-300" />
        {context === "result" ? "This plan used live data from all services." : "Live mode: you'll get real results."}
      </div>
    );
  }

  return (
    <div className="flex gap-3 rounded-2xl border border-amber-400/30 bg-amber-500/[0.07] px-4 py-3.5 text-left animate-rise">
      <FlaskConical className="mt-0.5 h-5 w-5 shrink-0 text-amber-300" />
      <div>
        <p className="text-sm font-semibold text-amber-100">
          {context === "result" ? "This plan uses demo data" : "Demo mode: results will be sample data"}
        </p>
        <p className="mt-0.5 text-xs leading-relaxed text-amber-100/70">
          Every agent returns sample output. Live data is enabled by the service operator.
        </p>
      </div>
    </div>
  );
}
