// ClaimGuard — one page, three columns, dark.
// Left: denial worklist. Center: live agent trace. Right: decision card.
import { useCallback, useEffect, useRef, useState } from "react";
import { getDenials, getStats, openTraceStream, uploadFeed, type FeedResult } from "./api/sse";
import ClaimList from "./components/ClaimList";
import ClaimModal from "./components/ClaimModal";
import DecisionPanel from "./components/DecisionPanel";
import ReportModal from "./components/ReportModal";
import StatsBar from "./components/StatsBar";
import TraceTimeline from "./components/TraceTimeline";
import type { DenialSummary, Route, Stats, TraceEvent } from "./types";

export default function App() {
  const [denials, setDenials] = useState<DenialSummary[]>([]);
  const [routes, setRoutes] = useState<Record<string, Route>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [processingId, setProcessingId] = useState<string | null>(null);
  const [batchRunning, setBatchRunning] = useState(false);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [detailDenial, setDetailDenial] = useState<DenialSummary | null>(null);
  const [showReports, setShowReports] = useState(false);
  const [feedResult, setFeedResult] = useState<FeedResult | null>(null);
  const closeStream = useRef<(() => void) | null>(null);

  const refreshStats = useCallback(() => {
    getStats().then(setStats).catch(() => {});
  }, []);

  useEffect(() => {
    getDenials().then(setDenials).catch(() => {});
    refreshStats();
    return () => closeStream.current?.();
  }, [refreshStats]);

  /** Stream one denial through the agent; resolves when the trace completes. */
  const processOne = useCallback(
    (denialId: string) =>
      new Promise<void>((resolve) => {
        closeStream.current?.();
        setSelectedId(denialId);
        setProcessingId(denialId);
        setEvents([]);
        closeStream.current = openTraceStream(
          denialId,
          (event) => {
            setEvents((prior) => [...prior, event]);
            if (event.type === "completed" && event.payload.route) {
              setRoutes((prior) => ({ ...prior, [denialId]: event.payload.route as Route }));
            }
          },
          () => {
            setProcessingId(null);
            refreshStats();
            resolve();
          },
        );
      }),
    [refreshStats],
  );

  const processAll = useCallback(async (denialIds: string[]) => {
    setBatchRunning(true);
    try {
      for (const denialId of denialIds) {
        await processOne(denialId);
      }
    } finally {
      setBatchRunning(false);
    }
  }, [processOne]);

  const handleUploadFeed = useCallback(async (file: File) => {
    const result = await uploadFeed(await file.text());
    setFeedResult(result);
    if (result.ok) getDenials().then(setDenials).catch(() => {});
  }, []);

  const selected = denials.find((d) => d.denial_id === selectedId) ?? null;

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <StatsBar stats={stats} onOpenReports={() => setShowReports(true)} />
      <main className="grid min-h-0 flex-1 grid-cols-[290px_minmax(0,1fr)_340px] overflow-hidden">
        <ClaimList
          denials={denials}
          routes={routes}
          selectedId={selectedId}
          processingId={processingId}
          batchRunning={batchRunning}
          onSelect={processOne}
          onProcessAll={processAll}
          onShowDetails={setDetailDenial}
          onUploadFeed={handleUploadFeed}
        />
        <TraceTimeline events={events} denial={selected} />
        <DecisionPanel
          key={selectedId ?? "none"}
          events={events}
          onApproved={refreshStats}
        />
      </main>
      {detailDenial && <ClaimModal denial={detailDenial} onClose={() => setDetailDenial(null)} />}
      {showReports && <ReportModal onClose={() => setShowReports(false)} />}
      {feedResult && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6 backdrop-blur-sm"
          onClick={() => setFeedResult(null)}
        >
          <div
            className="flash-in w-full max-w-lg rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className={`border-b border-zinc-800 px-5 py-3 text-sm font-bold ${feedResult.ok ? "text-emerald-400" : "text-rose-400"}`}>
              {feedResult.ok ? "✓ Import successful" : "✗ Import rejected — nothing was imported"}
            </div>
            <div className="max-h-72 overflow-y-auto p-5 text-xs">
              {feedResult.ok ? (
                <p className="text-zinc-300">
                  Added <span className="font-bold text-emerald-400">{feedResult.accepted?.claims} claim(s)</span> and{" "}
                  <span className="font-bold text-emerald-400">{feedResult.accepted?.denials} denial(s)</span> to the
                  worklist. Select them to triage live.
                </p>
              ) : (
                <ul className="space-y-1.5">
                  {feedResult.errors.map((err, i) => (
                    <li key={i} className="font-mono text-[11px] leading-snug text-rose-300">• {err}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="border-t border-zinc-800 px-5 py-2.5 text-right">
              <button
                onClick={() => setFeedResult(null)}
                className="rounded-lg bg-zinc-800 px-4 py-1.5 text-xs font-semibold text-zinc-200 hover:bg-zinc-700"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
