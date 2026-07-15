// Right column: decision card — root cause, chosen route (others struck
// through), citations as blockquotes, before→after fix diff, confidence bar,
// approve & resubmit.
import { useState } from "react";
import { approveDecision } from "../api/sse";
import type { Citation, Route, TraceEvent } from "../types";
import { money, ROUTE_BADGE, ROUTE_LABEL } from "../types";

const ALL_ROUTES: Route[] = ["auto_fix_resubmit", "appeal", "write_off", "human_review"];

interface FixOperation {
  field_path: string;
  op: string;
  old_value: unknown;
  new_value: unknown;
  reason: string;
}

function pick(events: TraceEvent[], type: string): TraceEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === type) return events[i];
  return undefined;
}

function DiffValue({ value }: { value: unknown }) {
  const text = value === null || value === undefined || value === "" ? "∅" : JSON.stringify(value);
  return <span className="font-mono">{text}</span>;
}

export default function DecisionPanel({
  events,
  onApproved,
}: {
  events: TraceEvent[];
  onApproved: () => void;
}) {
  const [approving, setApproving] = useState(false);
  const [approval, setApproval] = useState<Record<string, any> | null>(null);

  const rootCause = pick(events, "root_cause");
  const decision = pick(events, "decision");
  const fixEvent = pick(events, "fix_applied") ?? pick(events, "fix_validated") ?? pick(events, "fix_proposed");
  const resubmitted = pick(events, "resubmitted");
  const appealDrafted = pick(events, "appeal_drafted");
  const completed = pick(events, "completed");

  if (!decision) {
    return (
      <aside className="flex min-h-0 flex-col overflow-hidden border-l border-zinc-800">
        <div className="shrink-0 border-b border-zinc-800 px-4 py-2 text-xs font-semibold uppercase tracking-widest text-zinc-500">
          decision
        </div>
        <div className="mt-16 px-6 text-center text-sm text-zinc-600">
          The decision card fills in once the agent commits to a route.
        </div>
      </aside>
    );
  }

  const chosen = decision.payload.route as Route;
  const rejected: Record<string, string> = decision.payload.rejected_routes ?? {};
  const confidence = Number(decision.payload.confidence ?? 0);
  const citations: Citation[] = decision.payload.citations ?? [];
  const operations: FixOperation[] = fixEvent?.payload.fix?.operations ?? fixEvent?.payload.operations ?? [];
  const denialId = decision.denial_id;
  const alreadyResubmitted = resubmitted?.payload.status === "accepted";

  async function approve() {
    setApproving(true);
    try {
      setApproval(await approveDecision(denialId));
      onApproved();
    } finally {
      setApproving(false);
    }
  }

  return (
    <aside className="flex min-h-0 flex-col overflow-hidden border-l border-zinc-800">
      <div className="shrink-0 border-b border-zinc-800 px-4 py-2 text-xs font-semibold uppercase tracking-widest text-zinc-500">
        decision <span className="ml-2 font-mono normal-case text-zinc-400">{denialId}</span>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {rootCause && (
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">root cause</div>
            <div className="mt-1 flex items-start gap-2">
              <span className="rounded bg-rose-500/10 px-1.5 py-0.5 font-mono text-[10px] text-rose-300 ring-1 ring-rose-500/30">
                {rootCause.payload.category}
              </span>
            </div>
            <p className="mt-1.5 text-sm leading-snug text-zinc-300">{rootCause.payload.summary}</p>
          </div>
        )}

        <div>
          <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">route</div>
          <ul className="mt-1.5 space-y-1">
            {ALL_ROUTES.map((route) =>
              route === chosen ? (
                <li key={route} className={`rounded px-2 py-1 text-sm font-bold ring-1 ${ROUTE_BADGE[route]}`}>
                  ✓ {ROUTE_LABEL[route]}
                  <span className="ml-2 text-[11px] font-normal opacity-80">{decision.payload.rationale}</span>
                </li>
              ) : (
                <li key={route} className="px-2 py-0.5 text-xs text-zinc-600 line-through decoration-zinc-700">
                  {ROUTE_LABEL[route]}
                  {rejected[route] && (
                    <span className="ml-2 text-[10px] no-underline text-zinc-500">— {rejected[route]}</span>
                  )}
                </li>
              ),
            )}
          </ul>
          {decision.payload.guardrail_note && (
            <p className="mt-1.5 text-[11px] italic text-amber-400/80">⛊ {decision.payload.guardrail_note}</p>
          )}
        </div>

        {citations.length > 0 && (
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">evidence</div>
            {citations.map((citation) => (
              <blockquote
                key={citation.source_id}
                className="mt-1.5 border-l-2 border-violet-500/50 bg-violet-500/5 py-1.5 pl-3 pr-2"
              >
                <p className="text-[11px] italic leading-snug text-zinc-400">“{citation.quote}”</p>
                <footer className="mt-1 font-mono text-[10px] text-violet-400">— {citation.source_id}</footer>
              </blockquote>
            ))}
          </div>
        )}

        {operations.length > 0 && (
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">proposed fix</div>
            <div className="mt-1.5 space-y-1.5 rounded-lg bg-zinc-900 p-2.5 ring-1 ring-zinc-800">
              {operations.map((op, i) => (
                <div key={i} className="text-[11px]">
                  <span className="font-mono text-zinc-400">{op.field_path}</span>
                  <div className="mt-0.5 flex items-center gap-2 pl-2">
                    <span className="rounded bg-rose-500/10 px-1.5 py-0.5 text-rose-400 line-through">
                      <DiffValue value={op.old_value} />
                    </span>
                    <span className="text-zinc-600">→</span>
                    <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-400">
                      <DiffValue value={op.op === "add" ? op.new_value : op.new_value} />
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {appealDrafted && (
          <div>
            <div className="flex items-baseline justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">
                appeal letter
              </span>
              <button
                onClick={() => navigator.clipboard.writeText(appealDrafted.payload.letter)}
                className="rounded bg-sky-500/10 px-2 py-0.5 text-[10px] font-semibold text-sky-400 ring-1 ring-sky-500/30 hover:bg-sky-500/20"
              >
                copy
              </button>
            </div>
            <pre className="mt-1.5 max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg bg-zinc-900 p-2.5 font-sans text-[11px] leading-relaxed text-zinc-300 ring-1 ring-sky-500/20">
              {appealDrafted.payload.letter}
            </pre>
          </div>
        )}

        <div>
          <div className="flex items-baseline justify-between">
            <span className="text-[10px] font-semibold uppercase tracking-widest text-zinc-500">confidence</span>
            <span className="font-mono text-xs text-zinc-300">{(confidence * 100).toFixed(0)}%</span>
          </div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-zinc-800">
            <div
              className={`h-full rounded-full transition-all duration-700 ${
                confidence > 0.85 ? "bg-emerald-500" : confidence > 0.6 ? "bg-amber-500" : "bg-rose-500"
              }`}
              style={{ width: `${confidence * 100}%` }}
            />
          </div>
          <div className="mt-1 text-right text-[10px] text-zinc-600">
            {money(decision.payload.value_at_stake ?? 0)} at stake
          </div>
        </div>
      </div>

      <div className="shrink-0 border-t border-zinc-800 p-3">
        {approval ? (
          <div className={`rounded-lg px-3 py-2 text-center text-sm font-semibold ${
            approval.resubmit_status === "accepted" || alreadyResubmitted
              ? "bg-emerald-500/10 text-emerald-400 ring-1 ring-emerald-500/30"
              : "bg-zinc-800 text-zinc-300"
          }`}>
            ✓ approved{approval.resubmit_status ? ` — resubmission ${approval.resubmit_status}` : ""}
          </div>
        ) : alreadyResubmitted ? (
          <div className="rounded-lg bg-emerald-500/10 px-3 py-2 text-center text-sm font-semibold text-emerald-400 ring-1 ring-emerald-500/30">
            ⇪ auto-resubmitted — {resubmitted?.payload.payer_ack_code} · {resubmitted?.payload.trace_id}
          </div>
        ) : (
          <button
            onClick={approve}
            disabled={approving || !completed}
            className="w-full rounded-lg bg-emerald-600 py-2.5 text-sm font-bold text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
          >
            {approving ? "Submitting…" : "Approve & Resubmit"}
          </button>
        )}
      </div>
    </aside>
  );
}
