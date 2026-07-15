// Small chip for a citation source id, e.g. [NCCI-29881/99213]. Quote on hover.
import type { Citation } from "../types";

export default function CitationBadge({ citation }: { citation: Citation }) {
  return (
    <span
      title={citation.quote}
      className="inline-flex cursor-help items-center rounded bg-violet-500/10 px-1.5 py-0.5 font-mono text-[10px] text-violet-300 ring-1 ring-violet-500/30"
    >
      {citation.source_id}
    </span>
  );
}
