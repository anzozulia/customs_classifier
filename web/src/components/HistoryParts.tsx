/** Small pieces shared by the history list and the history detail view. */

import type { HistoryCode, Outcome } from "../lib/history";
import { OUTCOME_LABEL, formatCode } from "../lib/history";

const OUTCOME_STYLE: Record<Outcome, string> = {
  classified: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  clarification: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  error: "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
  pending: "bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
};

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  const style = OUTCOME_STYLE[outcome] ?? OUTCOME_STYLE.pending;
  const label = OUTCOME_LABEL[outcome] ?? outcome;
  return (
    <span className={`rounded-md px-2 py-0.5 text-xs font-medium ${style}`}>{label}</span>
  );
}

/**
 * `full_path` is always shown and `description` never stands alone: 2,473 of the 10,490
 * terminal descriptions in the dataset are literally "інші", so a bare description is not
 * an answer a broker can act on.
 */
export function CodeList({ codes }: { codes: HistoryCode[] }) {
  return (
    <ul className="space-y-2">
      {codes.map((code) => (
        <li key={code.code} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="code-chip">{formatCode(code.code)}</span>
          {code.is_primary === false && (
            <span className="text-xs text-slate-400">альтернатива</span>
          )}
          <span className="text-sm text-slate-700 dark:text-slate-300">{code.description}</span>
          <span className="w-full text-xs text-slate-500 dark:text-slate-400">
            {code.full_path}
          </span>
        </li>
      ))}
    </ul>
  );
}
