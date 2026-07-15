// Top bar: animated "$ recovered" counter + route tallies.
import { useEffect, useRef, useState } from "react";
import type { Stats } from "../types";
import { money, ROUTE_BADGE, ROUTE_LABEL, type Route } from "../types";

function useCountUp(target: number, duration = 900): number {
  const [value, setValue] = useState(target);
  const previous = useRef(target);
  useEffect(() => {
    const from = previous.current;
    if (from === target) return;
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      setValue(from + (target - from) * eased);
      if (progress < 1) frame = requestAnimationFrame(tick);
      else previous.current = target;
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);
  return value;
}

export default function StatsBar({
  stats,
  onOpenReports,
}: {
  stats: Stats | null;
  onOpenReports: () => void;
}) {
  const recovered = useCountUp(stats ? parseFloat(stats.dollars_recovered) : 0);
  const routes: Route[] = ["auto_fix_resubmit", "appeal", "write_off", "human_review"];
  return (
    <header className="flex items-center gap-6 border-b border-zinc-800 bg-zinc-900/60 px-5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="text-lg font-bold tracking-tight text-zinc-100">
          Claim<span className="text-emerald-400">Guard</span>
        </span>
        <span className="text-[11px] uppercase tracking-widest text-zinc-500">denial triage agent</span>
      </div>
      <div className="ml-auto flex items-center gap-5">
        {routes.map((route) => {
          const entries = stats?.route_details?.[route] ?? [];
          const total = entries.reduce((sum, e) => sum + parseFloat(e.total_denied), 0);
          return (
            <div key={route} className="group relative flex cursor-help items-center gap-1.5 text-xs">
              <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ring-1 ${ROUTE_BADGE[route]}`}>
                {ROUTE_LABEL[route]}
              </span>
              <span className="tabular-nums text-zinc-400">{stats?.route_counts[route] ?? 0}</span>
              {entries.length > 0 && (
                <div className="invisible absolute right-0 top-full z-50 mt-2 w-72 rounded-lg border border-zinc-700 bg-zinc-900 opacity-0 shadow-2xl transition-opacity duration-150 group-hover:visible group-hover:opacity-100">
                  <div className="flex items-baseline justify-between border-b border-zinc-800 px-3 py-2">
                    <span className={`rounded px-1.5 py-0.5 text-[9px] font-bold ring-1 ${ROUTE_BADGE[route]}`}>
                      {ROUTE_LABEL[route]}
                    </span>
                    <span className="font-mono text-[10px] tabular-nums text-zinc-400">
                      {entries.length} denial{entries.length === 1 ? "" : "s"} · {money(total)}
                    </span>
                  </div>
                  <div className="max-h-56 overflow-y-auto p-1.5">
                    {entries.map((e) => (
                      <div key={e.denial_id} className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-zinc-800/60">
                        <span className="font-mono text-[10px] text-zinc-200">{e.denial_id}</span>
                        <span className="min-w-0 flex-1 truncate text-[10px] text-zinc-500">
                          {e.payer_name}
                          {e.root_cause_category ? ` · ${e.root_cause_category}` : ""}
                        </span>
                        <span className="font-mono text-[10px] tabular-nums text-zinc-300">
                          {money(e.total_denied)}
                        </span>
                        {e.resubmit_status === "accepted" && (
                          <span title="resubmitted & accepted" className="text-[10px] text-emerald-400">✓</span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
        <div className="flex items-baseline gap-2 rounded-lg bg-emerald-500/10 px-4 py-1.5 ring-1 ring-emerald-500/30">
          <span className="text-[10px] uppercase tracking-widest text-emerald-500">recovered</span>
          <span className="text-xl font-bold tabular-nums text-emerald-400">{money(recovered)}</span>
        </div>
        <button
          onClick={onOpenReports}
          className="rounded-lg bg-zinc-800 px-3 py-2 text-xs font-semibold text-zinc-200 ring-1 ring-zinc-700 transition-colors hover:bg-zinc-700"
        >
          ⚡ Analytics
        </button>
      </div>
    </header>
  );
}
