// API client: EventSource over GET /api/process/{id} + JSON fetch helpers.
import type { Claim, DenialDetail, DenialSummary, Stats, TraceEvent, TraceEventType } from "../types";

const EVENT_TYPES: TraceEventType[] = [
  "started", "thought", "context_retrieved", "root_cause", "tool_call",
  "tool_result", "decision", "fix_proposed", "fix_validated", "fix_rejected",
  "fix_applied", "resubmitted", "appeal_drafted", "routed_to_human", "completed", "error",
];

/** Subscribe to the agent trace for one denial. Returns a close() handle.
 *  The stream self-closes after the `completed` event. */
export function openTraceStream(
  denialId: string,
  onEvent: (event: TraceEvent) => void,
  onClose: () => void,
): () => void {
  const source = new EventSource(`/api/process/${denialId}`);
  const finish = () => {
    source.close();
    onClose();
  };
  for (const type of EVENT_TYPES) {
    source.addEventListener(type, (raw) => {
      const event = JSON.parse((raw as MessageEvent).data) as TraceEvent;
      onEvent(event);
      if (type === "completed") finish();
    });
  }
  source.onerror = finish;
  return finish;
}

export async function getDenials(): Promise<DenialSummary[]> {
  return (await fetch("/api/denials")).json();
}

export async function getStats(): Promise<Stats> {
  return (await fetch("/api/stats")).json();
}

export async function approveDecision(denialId: string): Promise<Record<string, any>> {
  return (await fetch(`/api/approve/${denialId}`, { method: "POST" })).json();
}

export interface FeedResult {
  ok: boolean;
  accepted?: { claims: number; denials: number };
  errors: string[];
}

/** Upload a live-feed JSON file ({claims: [...], denials: [...]}). */
export async function uploadFeed(fileText: string): Promise<FeedResult> {
  let body: unknown;
  try {
    body = JSON.parse(fileText);
  } catch {
    return { ok: false, errors: ["File is not valid JSON."] };
  }
  const response = await fetch("/api/feed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    const detail = data?.detail;
    const errors = Array.isArray(detail?.errors)
      ? detail.errors
      : [typeof detail === "string" ? detail : "Feed rejected."];
    return { ok: false, errors };
  }
  return { ok: true, accepted: data.accepted, errors: [] };
}

export async function getClaim(claimId: string): Promise<Claim> {
  return (await fetch(`/api/claims/${claimId}`)).json();
}

export async function getDenial(denialId: string): Promise<DenialDetail> {
  return (await fetch(`/api/denials/${denialId}`)).json();
}
