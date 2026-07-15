// One row in the worklist: id, payer, CARCs, $, route badge, and a ⓘ details popup.
import type { DenialSummary, Route } from "../types";
import { money, ROUTE_BADGE, ROUTE_LABEL } from "../types";

interface Props {
  denial: DenialSummary;
  route?: Route;
  selected: boolean;
  processing: boolean;
  onClick: () => void;
  onShowDetails: () => void;
}

export default function DenialCard({ denial, route, selected, processing, onClick, onShowDetails }: Props) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => e.key === "Enter" && onClick()}
      className={`w-full cursor-pointer rounded-lg border px-3 py-2 text-left transition-colors ${
        selected
          ? "border-emerald-500/50 bg-emerald-500/5"
          : "border-zinc-800 bg-zinc-900/40 hover:border-zinc-700 hover:bg-zinc-900"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 font-mono text-xs font-semibold text-zinc-200">
          {denial.denial_id}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onShowDetails();
            }}
            title="View claim details (non-PHI)"
            aria-label={`View details for ${denial.denial_id}`}
            className="rounded-full px-1 text-[11px] text-zinc-500 hover:bg-zinc-700 hover:text-zinc-100"
          >
            ⓘ
          </button>
        </span>
        {processing ? (
          <span className="animate-pulse text-[10px] font-semibold uppercase tracking-wider text-emerald-400">
            triaging…
          </span>
        ) : route ? (
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ${ROUTE_BADGE[route]}`}>
            {ROUTE_LABEL[route]}
          </span>
        ) : (
          <span className="text-[10px] uppercase tracking-wider text-zinc-600">pending</span>
        )}
      </div>
      <div className="mt-1 flex items-center justify-between text-[11px] text-zinc-500">
        <span className="truncate">{denial.payer_name}</span>
        <span className="tabular-nums text-zinc-400">{money(denial.total_denied)}</span>
      </div>
      <div className="mt-1 flex gap-1">
        {denial.carcs.map((carc) => (
          <span key={carc} className="rounded bg-rose-500/10 px-1 py-px font-mono text-[10px] text-rose-400 ring-1 ring-rose-500/20">
            CO-{carc}
          </span>
        ))}
      </div>
    </div>
  );
}
