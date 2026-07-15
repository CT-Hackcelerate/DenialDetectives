// Popup with the full (non-PHI) claim + denial detail.
// Identifiers are display-masked; no names, DOB, or addresses exist in the data.
import { useEffect, useState } from "react";
import { getClaim, getDenial } from "../api/sse";
import type { Claim, DenialDetail, DenialSummary } from "../types";
import { maskId, money } from "../types";

function Field({ label, value, mono = true }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[9px] font-semibold uppercase tracking-widest text-zinc-500">{label}</div>
      <div className={`mt-0.5 text-xs text-zinc-200 ${mono ? "font-mono" : ""}`}>{value}</div>
    </div>
  );
}

export default function ClaimModal({
  denial,
  onClose,
}: {
  denial: DenialSummary;
  onClose: () => void;
}) {
  const [claim, setClaim] = useState<Claim | null>(null);
  const [detail, setDetail] = useState<DenialDetail | null>(null);

  useEffect(() => {
    getClaim(denial.claim_id).then(setClaim).catch(() => {});
    getDenial(denial.denial_id).then(setDetail).catch(() => {});
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [denial, onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-6 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flash-in flex max-h-[85vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-5 py-3">
          <div>
            <span className="font-mono text-sm font-bold text-zinc-100">{denial.claim_id}</span>
            <span className="ml-3 text-xs text-zinc-500">{denial.payer_name}</span>
          </div>
          <button
            onClick={onClose}
            className="rounded px-2 py-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
            aria-label="close"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto p-5">
          {!claim || !detail ? (
            <div className="py-10 text-center text-sm text-zinc-600">loading…</div>
          ) : (
            <>
              <div className="grid grid-cols-4 gap-x-4 gap-y-3">
                <Field label="patient ref" value={claim.patient_ref} />
                <Field label="member id" value={maskId(claim.subscriber_id)} />
                <Field label="date of service" value={claim.date_of_service} />
                <Field label="submitted" value={claim.date_submitted} />
                <Field label="provider" value={claim.provider_name} mono={false} />
                <Field label="npi" value={claim.provider_npi} />
                <Field label="prior auth" value={claim.prior_auth_number ?? "—"} />
                <Field label="revision" value={String(claim.revision)} />
              </div>

              <div>
                <div className="text-[9px] font-semibold uppercase tracking-widest text-zinc-500">diagnoses (ICD-10)</div>
                <div className="mt-1 flex flex-wrap gap-1">
                  {claim.diagnoses.map((dx) => (
                    <span key={dx} className="rounded bg-sky-500/10 px-1.5 py-0.5 font-mono text-[11px] text-sky-300 ring-1 ring-sky-500/25">
                      {dx}
                    </span>
                  ))}
                </div>
              </div>

              <div>
                <div className="text-[9px] font-semibold uppercase tracking-widest text-zinc-500">service lines</div>
                <table className="mt-1.5 w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500">
                      <th className="py-1.5 pr-2 font-semibold">#</th>
                      <th className="py-1.5 pr-2 font-semibold">cpt</th>
                      <th className="py-1.5 pr-2 font-semibold">mods</th>
                      <th className="py-1.5 pr-2 font-semibold">dx ptr</th>
                      <th className="py-1.5 pr-2 font-semibold">units</th>
                      <th className="py-1.5 pr-2 font-semibold">pos</th>
                      <th className="py-1.5 text-right font-semibold">charge</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono">
                    {claim.lines.map((line) => (
                      <tr key={line.line_number} className="border-b border-zinc-800/50">
                        <td className="py-1.5 pr-2 text-zinc-500">{line.line_number}</td>
                        <td className="py-1.5 pr-2 text-zinc-200">{line.cpt_hcpcs}</td>
                        <td className="py-1.5 pr-2 text-emerald-400">{line.modifiers.join(", ") || "—"}</td>
                        <td className="py-1.5 pr-2 text-sky-300">{line.icd10_pointers.join(", ") || "—"}</td>
                        <td className="py-1.5 pr-2 text-zinc-400">{line.units}</td>
                        <td className="py-1.5 pr-2 text-zinc-400">{line.place_of_service ?? "—"}</td>
                        <td className="py-1.5 text-right text-zinc-200">{money(line.charge)}</td>
                      </tr>
                    ))}
                    <tr>
                      <td colSpan={6} className="py-1.5 pr-2 text-right text-[10px] uppercase tracking-wider text-zinc-500">
                        total charge
                      </td>
                      <td className="py-1.5 text-right font-bold text-zinc-100">{money(claim.total_charge)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <div className="rounded-lg border border-rose-500/25 bg-rose-500/5 p-3">
                <div className="flex items-baseline justify-between">
                  <div className="text-[9px] font-semibold uppercase tracking-widest text-rose-400">
                    835 denial — remit {detail.remit_date}
                  </div>
                  <div className="font-mono text-xs font-bold text-rose-300">{money(detail.total_denied)} denied</div>
                </div>
                <div className="mt-2 space-y-1 font-mono text-[11px] text-zinc-300">
                  {detail.adjustments.map((adj, i) => (
                    <div key={i}>
                      {adj.group_code}-{adj.carc}
                      {adj.rarc ? ` / ${adj.rarc}` : ""} · {money(adj.amount)}
                      {adj.line_number != null ? ` · line ${adj.line_number}` : " · claim level"}
                    </div>
                  ))}
                </div>
                {detail.remit_note && (
                  <p className="mt-2 text-[11px] italic text-zinc-400">“{detail.remit_note}”</p>
                )}
                {detail.payer_context && (
                  <p className="mt-1.5 text-[11px] text-amber-300/90">📎 {detail.payer_context}</p>
                )}
              </div>
            </>
          )}
        </div>

        <div className="shrink-0 border-t border-zinc-800 px-5 py-2 text-center text-[10px] text-zinc-600">
          Synthetic data — contains no PHI. Member identifier is display-masked.
        </div>
      </div>
    </div>
  );
}
