// Left column: denial worklist + "Process all" + import + filters.
import { useMemo, useRef, useState } from "react";
import type { DenialSummary, Route } from "../types";
import { ROUTE_LABEL } from "../types";
import DenialCard from "./DenialCard";

interface Props {
  denials: DenialSummary[];
  routes: Record<string, Route>;
  selectedId: string | null;
  processingId: string | null;
  batchRunning: boolean;
  onSelect: (denialId: string) => void;
  onProcessAll: (denialIds: string[]) => void;
  onShowDetails: (denial: DenialSummary) => void;
  onUploadFeed: (file: File) => void;
}

type StatusFilter = "all" | "pending" | Route;

export default function ClaimList({
  denials, routes, selectedId, processingId, batchRunning, onSelect, onProcessAll, onShowDetails, onUploadFeed,
}: Props) {
  const done = Object.keys(routes).length;
  const fileInput = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [payer, setPayer] = useState("all");
  const [status, setStatus] = useState<StatusFilter>("all");

  const payers = useMemo(
    () => Array.from(new Set(denials.map((d) => d.payer_name))).sort(),
    [denials],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return denials.filter((d) => {
      if (payer !== "all" && d.payer_name !== payer) return false;
      const route = routes[d.denial_id];
      if (status === "pending" && route) return false;
      if (status !== "all" && status !== "pending" && route !== status) return false;
      if (q) {
        const haystack = `${d.denial_id} ${d.claim_id} ${d.carcs.join(" ")}`.toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }, [denials, routes, query, payer, status]);

  // Worklist order: the one being triaged first, untouched denials next,
  // processed ones sink to the bottom. Stable within each group.
  const rank = (d: DenialSummary) =>
    d.denial_id === processingId ? 0 : routes[d.denial_id] ? 2 : 1;
  const ordered = [...filtered].sort((a, b) => rank(a) - rank(b));

  const isFiltered = filtered.length !== denials.length;

  return (
    <aside className="flex min-h-0 flex-col overflow-hidden border-r border-zinc-800">
      <div className="shrink-0 space-y-2 border-b border-zinc-800 p-3">
        <div className="flex gap-2">
          <button
            onClick={() => onProcessAll(ordered.map((d) => d.denial_id))}
            disabled={batchRunning || processingId !== null || ordered.length === 0}
            className="flex-1 rounded-lg bg-emerald-600 py-2 text-sm font-semibold text-white transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
          >
            {batchRunning
              ? `Processing… ${done}/${denials.length}`
              : isFiltered
                ? `▶ Process filtered (${ordered.length})`
                : "▶ Process all"}
          </button>
          <button
            onClick={() => fileInput.current?.click()}
            title="Import claims — upload a JSON batch of claims + denials"
            className="rounded-lg bg-zinc-800 px-3 py-2 text-sm font-semibold text-zinc-200 ring-1 ring-zinc-700 transition-colors hover:bg-zinc-700"
          >
            📥 Import
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onUploadFeed(file);
              e.target.value = ""; // allow re-uploading the same file
            }}
          />
        </div>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search id / CARC…"
          className="w-full rounded-lg bg-zinc-900 px-2.5 py-1.5 text-xs text-zinc-200 placeholder-zinc-600 ring-1 ring-zinc-800 focus:outline-none focus:ring-emerald-500/50"
        />
        <div className="flex gap-2">
          <select
            value={payer}
            onChange={(e) => setPayer(e.target.value)}
            className="min-w-0 flex-1 rounded-lg bg-zinc-900 px-1.5 py-1.5 text-[11px] text-zinc-300 ring-1 ring-zinc-800 focus:outline-none"
          >
            <option value="all">All payers</option>
            {payers.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as StatusFilter)}
            className="min-w-0 flex-1 rounded-lg bg-zinc-900 px-1.5 py-1.5 text-[11px] text-zinc-300 ring-1 ring-zinc-800 focus:outline-none"
          >
            <option value="all">All statuses</option>
            <option value="pending">Pending</option>
            {(Object.keys(ROUTE_LABEL) as Route[]).map((r) => (
              <option key={r} value={r}>{ROUTE_LABEL[r]}</option>
            ))}
          </select>
        </div>
        {isFiltered && (
          <div className="flex items-center justify-between text-[10px] text-zinc-500">
            <span>{ordered.length} of {denials.length} denials</span>
            <button
              onClick={() => { setQuery(""); setPayer("all"); setStatus("all"); }}
              className="text-emerald-500 hover:text-emerald-400"
            >
              clear filters
            </button>
          </div>
        )}
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
        {ordered.length === 0 && (
          <div className="mt-10 text-center text-xs text-zinc-600">No denials match the filters.</div>
        )}
        {ordered.map((denial) => (
          <DenialCard
            key={denial.denial_id}
            denial={denial}
            route={routes[denial.denial_id]}
            selected={denial.denial_id === selectedId}
            processing={denial.denial_id === processingId}
            onClick={() => onSelect(denial.denial_id)}
            onShowDetails={() => onShowDetails(denial)}
          />
        ))}
      </div>
    </aside>
  );
}
