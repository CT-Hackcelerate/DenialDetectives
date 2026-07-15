// Center column: the live agent trace. Thoughts type char-by-char in italics,
// tool calls render in cyan mono, and the moment the agent's diagnosed root
// cause contradicts the payer's stated CARC reason gets a spotlight banner.
import { useEffect, useRef, useState } from "react";
import type { DenialSummary, TraceEvent } from "../types";
import { CARC_STATED_CATEGORY } from "../types";
import CitationBadge from "./CitationBadge";

function Typewriter({ text }: { text: string }) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (shown >= text.length) return;
    const step = Math.max(1, Math.ceil(text.length / 180)); // ~2s max per thought
    const timer = setTimeout(() => setShown((n) => Math.min(text.length, n + step)), 14);
    return () => clearTimeout(timer);
  }, [shown, text]);
  const done = shown >= text.length;
  return (
    <span>
      {text.slice(0, shown)}
      {!done && <span className="caret text-emerald-400">▌</span>}
    </span>
  );
}

function summarizeInput(input: Record<string, any> | undefined): string {
  if (!input) return "";
  const json = JSON.stringify(input);
  return json.length > 90 ? json.slice(0, 87) + "…" : json;
}

function EventLine({ event, denial }: { event: TraceEvent; denial: DenialSummary }) {
  switch (event.type) {
    case "started":
      return <div className="text-[11px] uppercase tracking-widest text-zinc-600">— {event.message} —</div>;

    case "thought":
      return (
        <div className="italic leading-relaxed text-zinc-300">
          <Typewriter text={event.message} />
        </div>
      );

    case "tool_call":
      return (
        <div className="font-mono text-[12px] text-cyan-400">
          <span className="text-cyan-600">▸ </span>
          {event.payload.tool}
          <span className="text-cyan-600">({summarizeInput(event.payload.input)})</span>
        </div>
      );

    case "tool_result":
      return (
        <div className="pl-4 font-mono text-[11px] text-zinc-600">
          ↳ {event.payload.tool} returned
        </div>
      );

    case "context_retrieved":
      return (
        <div className="pl-4 font-mono text-[11px] text-violet-400/80">↳ {event.message}</div>
      );

    case "root_cause": {
      const stated = CARC_STATED_CATEGORY[denial.carcs[0]];
      const rejectsStated = stated !== undefined && event.payload.category !== stated;
      return (
        <div
          className={`flash-in rounded-lg border p-3 ${
            rejectsStated
              ? "border-amber-500/60 bg-amber-500/10 shadow-[0_0_24px_-6px] shadow-amber-500/40"
              : "border-zinc-700 bg-zinc-900"
          }`}
        >
          {rejectsStated && (
            <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-wider text-amber-400">
              <span className="text-base">⚡</span> stated reason rejected
              <span className="ml-auto rounded bg-amber-500/20 px-2 py-0.5 font-mono text-[10px] normal-case">
                remit said "{stated}" → agent found "{event.payload.category}"
              </span>
            </div>
          )}
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">root cause</div>
          <div className="mt-1 text-sm text-zinc-200">{event.payload.summary}</div>
          <div className="mt-2 flex flex-wrap gap-1">
            {event.citations.map((c) => <CitationBadge key={c.source_id + event.event_id} citation={c} />)}
          </div>
        </div>
      );
    }

    case "fix_proposed":
      return <div className="font-mono text-[12px] text-emerald-500/80">✎ {event.message}</div>;
    case "fix_validated":
      return <div className="font-mono text-[12px] text-emerald-400">✓ {event.message}</div>;
    case "fix_rejected":
      return <div className="font-mono text-[12px] text-rose-400">✗ {event.message}</div>;
    case "fix_applied":
      return <div className="font-mono text-[12px] text-emerald-400">✓ {event.message}</div>;
    case "resubmitted":
      return (
        <div className={`font-mono text-[12px] ${event.payload.status === "accepted" ? "text-emerald-400" : "text-rose-400"}`}>
          ⇪ {event.message}
        </div>
      );

    case "appeal_drafted":
      return (
        <div className="flash-in rounded-lg border border-sky-500/40 bg-sky-500/5 p-3">
          <div className="mb-2 text-xs font-bold uppercase tracking-wider text-sky-400">
            ✉ appeal letter drafted
          </div>
          <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap font-sans text-[11px] leading-relaxed text-zinc-300">
            {event.payload.letter}
          </pre>
        </div>
      );

    case "routed_to_human":
      return (
        <div className="rounded border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-xs text-amber-300">
          ⛊ {event.message}
        </div>
      );

    case "decision":
      return (
        <div className="text-sm font-semibold text-zinc-100">
          ⚖ {event.message}
        </div>
      );

    case "completed":
      return <div className="text-[11px] uppercase tracking-widest text-zinc-600">— {event.message} —</div>;

    case "error":
      return <div className="text-xs text-rose-400">⚠ {event.message}</div>;

    default:
      return null;
  }
}

export default function TraceTimeline({
  events,
  denial,
}: {
  events: TraceEvent[];
  denial: DenialSummary | null;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  });

  return (
    <section className="flex min-h-0 flex-col overflow-hidden">
      <div className="shrink-0 border-b border-zinc-800 px-4 py-2 text-xs font-semibold uppercase tracking-widest text-zinc-500">
        agent trace {denial && <span className="ml-2 font-mono normal-case text-zinc-400">{denial.denial_id}</span>}
      </div>
      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto p-4">
        {events.length === 0 && (
          <div className="mt-16 text-center text-sm text-zinc-600">
            Select a denial — or hit <span className="text-emerald-500">Process all</span> — to watch the agent work.
          </div>
        )}
        {events.map((event) => (
          denial && <EventLine key={event.event_id} event={event} denial={denial} />
        ))}
      </div>
    </section>
  );
}
