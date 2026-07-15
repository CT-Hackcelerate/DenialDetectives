// TypeScript mirror of the backend models (the slices the UI consumes).

export type Route = "auto_fix_resubmit" | "appeal" | "write_off" | "human_review";

export type TraceEventType =
  | "started"
  | "thought"
  | "context_retrieved"
  | "root_cause"
  | "tool_call"
  | "tool_result"
  | "decision"
  | "fix_proposed"
  | "fix_validated"
  | "fix_rejected"
  | "fix_applied"
  | "resubmitted"
  | "appeal_drafted"
  | "routed_to_human"
  | "completed"
  | "error";

export interface Citation {
  source_type: string;
  source_id: string;
  quote: string;
  chroma_doc_id?: string | null;
}

export interface TraceEvent {
  event_id: string;
  denial_id: string;
  seq: number;
  type: TraceEventType;
  message: string;
  payload: Record<string, any>;
  citations: Citation[];
  ts: string;
}

export interface ClaimLine {
  line_number: number;
  cpt_hcpcs: string;
  modifiers: string[];
  icd10_pointers: string[];
  units: number;
  charge: string;
  place_of_service: string | null;
}

export interface Claim {
  claim_id: string;
  payer_id: string;
  payer_name: string;
  provider_npi: string;
  provider_name: string;
  patient_ref: string;
  subscriber_id: string;
  date_of_service: string;
  date_submitted: string;
  prior_auth_number: string | null;
  diagnoses: string[];
  lines: ClaimLine[];
  total_charge: string;
  revision: number;
}

export interface Adjustment {
  group_code: string;
  carc: string;
  rarc: string | null;
  amount: string;
  line_number: number | null;
}

export interface DenialDetail {
  denial_id: string;
  claim_id: string;
  payer_id: string;
  payer_name: string;
  remit_date: string;
  adjustments: Adjustment[];
  total_denied: string;
  remit_note: string | null;
  payer_context: string | null;
}

/** Mask an identifier for display: SUB123456789 -> SUB•••••6789. */
export function maskId(id: string): string {
  if (id.length <= 7) return id.slice(0, 2) + "•••";
  return id.slice(0, 3) + "•••••" + id.slice(-4);
}

export interface DenialSummary {
  denial_id: string;
  claim_id: string;
  payer_name: string;
  remit_date: string;
  total_denied: string;
  carcs: string[];
}

export interface RouteDetailEntry {
  denial_id: string;
  payer_name: string;
  total_denied: string;
  root_cause_category: string | null;
  resubmit_status: string | null;
}

export interface Stats {
  denials_processed: number;
  dollars_recovered: string;
  dollars_processed: string;
  route_counts: Record<string, number>;
  route_details: Record<string, RouteDetailEntry[]>;
  lessons_learned: number;
}

export const ROUTE_LABEL: Record<Route, string> = {
  auto_fix_resubmit: "AUTO FIX",
  appeal: "APPEAL",
  write_off: "WRITE OFF",
  human_review: "HUMAN",
};

// badge colors: green / blue / grey / amber
export const ROUTE_BADGE: Record<Route, string> = {
  auto_fix_resubmit: "bg-emerald-500/15 text-emerald-400 ring-emerald-500/30",
  appeal: "bg-sky-500/15 text-sky-400 ring-sky-500/30",
  write_off: "bg-zinc-500/15 text-zinc-400 ring-zinc-500/30",
  human_review: "bg-amber-500/15 text-amber-400 ring-amber-500/30",
};

// What each CARC *claims* the problem is — used to spotlight the moment the
// agent's diagnosed root cause contradicts the payer's stated reason.
export const CARC_STATED_CATEGORY: Record<string, string> = {
  "1": "patient_responsibility",
  "2": "patient_responsibility",
  "3": "patient_responsibility",
  "4": "coding_mismatch",
  "11": "coding_mismatch",
  "16": "missing_info",
  "18": "duplicate",
  "22": "coordination_of_benefits",
  "27": "non_covered",
  "29": "timely_filing",
  "45": "contractual",
  "50": "medical_necessity",
  "96": "non_covered",
  "97": "bundling_ncci",
  "197": "auth_required",
  "198": "auth_required",
  "236": "bundling_ncci",
};

export function money(value: string | number): string {
  const n = typeof value === "string" ? parseFloat(value) : value;
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}
