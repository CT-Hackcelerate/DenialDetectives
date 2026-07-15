// Payer-wise denial analytics overlay.
// Chart discipline: identity is carried by text row labels (bars use ONE
// sequential hue for magnitude); route splits reuse the app's status colors
// with a legend, direct labels, and 2px gaps; win-rate bars carry % labels
// (emerald/rose alone is not CVD-safe).
import { useEffect, useState } from "react";
import { money, ROUTE_LABEL, type Route } from "../types";

interface PayerRow {
  payer: string;
  denials: number;
  denied: string;
  recovered: string;
  processed: number;
  avg_remit_lag_days: number | null;
  route_counts: Record<string, number>;
  top_carcs: { carc: string; count: number; denied: string; description: string }[];
  fix_history: { paid: number; denied_again: number };
}

interface Report {
  totals: { denials: number; denied: string; recovered: string; processed: number; lessons_learned: number };
  payers: PayerRow[];
  carcs: { carc: string; count: number; denied: string; description: string }[];
}

const ROUTE_FILL: Record<Route, string> = {
  auto_fix_resubmit: "#34D399",
  appeal: "#38BDF8",
  write_off: "#A1A1AA",
  human_review: "#FBBF24",
};
const ROUTES: Route[] = ["auto_fix_resubmit", "appeal", "write_off", "human_review"];

function Tile({ label, value, accent = "text-zinc-100" }: { label: string; value: string; accent?: string }) {
  return (
    <div className="rounded-lg bg-zinc-900 px-4 py-3 ring-1 ring-zinc-800">
      <div className="text-[9px] font-semibold uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={`mt-1 text-xl font-bold tabular-nums ${accent}`}>{value}</div>
    </div>
  );
}

function Bar({ fraction, color, label }: { fraction: number; color: string; label?: string }) {
  return (
    <div className="h-3.5 w-full rounded-[4px] bg-zinc-800/60" title={label}>
      <div
        className="h-full rounded-[4px] transition-all duration-500"
        style={{ width: `${Math.max(2, fraction * 100)}%`, background: color }}
      />
    </div>
  );
}

function RouteSplit({ counts }: { counts: Record<string, number> }) {
  const total = ROUTES.reduce((n, r) => n + (counts[r] ?? 0), 0);
  if (total === 0) return <div className="text-[10px] text-zinc-600">not processed yet</div>;
  return (
    <div className="flex h-3.5 w-full gap-[2px]">
      {ROUTES.filter((r) => counts[r]).map((r) => (
        <div
          key={r}
          title={`${ROUTE_LABEL[r]}: ${counts[r]}`}
          className="flex items-center justify-center rounded-[3px] text-[9px] font-bold text-zinc-950"
          style={{ width: `${(counts[r] / total) * 100}%`, background: ROUTE_FILL[r], minWidth: 14 }}
        >
          {counts[r]}
        </div>
      ))}
    </div>
  );
}

export default function ReportModal({ onClose }: { onClose: () => void }) {
  const [report, setReport] = useState<Report | null>(null);

  useEffect(() => {
    fetch("/api/report").then((r) => r.json()).then(setReport).catch(() => {});
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const maxPayerDenied = report ? Math.max(...report.payers.map((p) => parseFloat(p.denied)), 1) : 1;
  const maxCarcDenied = report ? Math.max(...report.carcs.map((c) => parseFloat(c.denied)), 1) : 1;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6 backdrop-blur-sm" onClick={onClose}>
      <div
        className="flash-in flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-xl border border-zinc-700 bg-zinc-950 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-5 py-3">
          <span className="text-sm font-bold uppercase tracking-widest text-zinc-300">⚡ Denial analytics</span>
          <button onClick={onClose} className="rounded px-2 py-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200" aria-label="close">✕</button>
        </div>

        <div className="flex-1 space-y-6 overflow-y-auto p-5">
          {!report ? (
            <div className="py-16 text-center text-sm text-zinc-600">loading…</div>
          ) : (
            <>
              <div className="grid grid-cols-5 gap-3">
                <Tile label="denials" value={String(report.totals.denials)} />
                <Tile label="$ denied" value={money(report.totals.denied)} accent="text-rose-400" />
                <Tile label="$ recovered" value={money(report.totals.recovered)} accent="text-emerald-400" />
                <Tile label="processed" value={`${report.totals.processed}/${report.totals.denials}`} />
                <Tile label="lessons learned" value={String(report.totals.lessons_learned)} accent="text-violet-400" />
              </div>

              {/* payer scorecards */}
              <section>
                <div className="mb-2 flex items-baseline justify-between">
                  <h3 className="text-[11px] font-bold uppercase tracking-widest text-zinc-400">Payer scorecard</h3>
                  <div className="flex gap-3">
                    {ROUTES.map((r) => (
                      <span key={r} className="flex items-center gap-1 text-[9px] uppercase tracking-wider text-zinc-500">
                        <span className="h-2 w-2 rounded-sm" style={{ background: ROUTE_FILL[r] }} />
                        {ROUTE_LABEL[r]}
                      </span>
                    ))}
                  </div>
                </div>
                <div className="overflow-hidden rounded-lg ring-1 ring-zinc-800">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="bg-zinc-900 text-left text-[9px] uppercase tracking-widest text-zinc-500">
                        <th className="px-3 py-2 font-semibold">payer</th>
                        <th className="w-[26%] px-3 py-2 font-semibold">$ denied</th>
                        <th className="px-3 py-2 text-right font-semibold">claims</th>
                        <th className="px-3 py-2 font-semibold">top reason</th>
                        <th className="w-[18%] px-3 py-2 font-semibold">routes</th>
                        <th className="px-3 py-2 text-right font-semibold">recovered</th>
                        <th className="px-3 py-2 text-right font-semibold">lag*</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.payers.map((p) => (
                        <tr key={p.payer} className="border-t border-zinc-800/70 hover:bg-zinc-900/50">
                          <td className="px-3 py-2.5 font-semibold text-zinc-200">{p.payer}</td>
                          <td className="px-3 py-2.5">
                            <div className="flex items-center gap-2">
                              <Bar fraction={parseFloat(p.denied) / maxPayerDenied} color="#FB7185"
                                   label={`${p.payer}: ${money(p.denied)} denied`} />
                              <span className="w-20 shrink-0 text-right font-mono tabular-nums text-zinc-300">{money(p.denied)}</span>
                            </div>
                          </td>
                          <td className="px-3 py-2.5 text-right font-mono tabular-nums text-zinc-400">{p.denials}</td>
                          <td className="px-3 py-2.5">
                            {p.top_carcs[0] && (
                              <span title={p.top_carcs[0].description}
                                    className="cursor-help rounded bg-rose-500/10 px-1.5 py-0.5 font-mono text-[10px] text-rose-300 ring-1 ring-rose-500/25">
                                CARC {p.top_carcs[0].carc} ×{p.top_carcs[0].count}
                              </span>
                            )}
                          </td>
                          <td className="px-3 py-2.5"><RouteSplit counts={p.route_counts} /></td>
                          <td className="px-3 py-2.5 text-right font-mono tabular-nums text-emerald-400">{money(p.recovered)}</td>
                          <td className="px-3 py-2.5 text-right font-mono tabular-nums text-zinc-500">
                            {p.avg_remit_lag_days != null ? `${p.avg_remit_lag_days}d` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="mt-1 text-[9px] text-zinc-600">* average days from date of service to remittance</div>
              </section>

              <div className="grid grid-cols-2 gap-6">
                {/* CARC leaderboard */}
                <section>
                  <h3 className="mb-2 text-[11px] font-bold uppercase tracking-widest text-zinc-400">
                    Top denial reasons by $
                  </h3>
                  <div className="space-y-2.5">
                    {report.carcs.slice(0, 7).map((c) => (
                      <div key={c.carc} title={c.description}>
                        <div className="mb-0.5 flex items-baseline justify-between text-[11px]">
                          <span className="font-mono text-zinc-300">CARC {c.carc}
                            <span className="ml-2 font-sans text-[10px] text-zinc-500">
                              {c.description.length > 46 ? c.description.slice(0, 44) + "…" : c.description}
                            </span>
                          </span>
                          <span className="font-mono tabular-nums text-zinc-400">{money(c.denied)} · ×{c.count}</span>
                        </div>
                        <Bar fraction={parseFloat(c.denied) / maxCarcDenied} color="#FB7185" />
                      </div>
                    ))}
                  </div>
                </section>

                {/* fix win rate */}
                <section>
                  <h3 className="mb-2 text-[11px] font-bold uppercase tracking-widest text-zinc-400">
                    Historical fix win-rate <span className="normal-case text-zinc-600">(past resubmissions)</span>
                  </h3>
                  <div className="space-y-3">
                    {report.payers.map((p) => {
                      const { paid, denied_again } = p.fix_history;
                      const total = paid + denied_again;
                      const rate = total ? Math.round((paid / total) * 100) : null;
                      return (
                        <div key={p.payer}>
                          <div className="mb-0.5 flex items-baseline justify-between text-[11px]">
                            <span className="text-zinc-300">{p.payer}</span>
                            <span className="font-mono tabular-nums text-zinc-400">
                              {rate != null ? `${rate}% paid (${paid}/${total})` : "no history"}
                            </span>
                          </div>
                          {total > 0 && (
                            <div className="flex h-3.5 w-full gap-[2px]">
                              <div title={`paid: ${paid}`} className="rounded-[3px]"
                                   style={{ width: `${(paid / total) * 100}%`, background: "#34D399" }} />
                              {denied_again > 0 && (
                                <div title={`denied again: ${denied_again}`} className="rounded-[3px]"
                                     style={{ width: `${(denied_again / total) * 100}%`, background: "#FB7185" }} />
                              )}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                  <p className="mt-3 text-[10px] italic text-zinc-600">
                    The agent checks this history (resubmission_history tool) before proposing any fix.
                  </p>
                </section>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
