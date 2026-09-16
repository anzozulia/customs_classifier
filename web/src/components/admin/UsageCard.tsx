/**
 * The spend dashboard — and, because there are no caps by decision, the ONLY place the
 * author finds out what a demo day cost.
 *
 * That makes honesty the whole design brief:
 *   • cost first and largest; counts are context, not the headline;
 *   • a field the server did not send prints «—», never «$0.00» (see `formatUsd`);
 *   • runs whose model is missing from the price table are counted OUT of the total and
 *     said out loud, so the number reads as a floor rather than as the truth;
 *   • every label says which window and which population it means. The server aggregates
 *     per window — the outcome mix below is all time, and a "user" is an account that ran
 *     at least one classification, not an account that exists — so the panel says so
 *     instead of letting the reader assume the flattering interpretation.
 *
 * "Today" is 00:00 UTC, which is 03:00 in Kyiv. The server sends the boundary it actually
 * used and it is in the tile's tooltip; the label carries the (UTC) so the difference is
 * visible without hovering.
 */

import { OutcomeBadge } from "../HistoryParts";
import type { AdminUsage, UsagePeriod } from "../../lib/admin";
import { formatInt, formatTokens, formatUsd } from "../../lib/admin";
import type { Outcome } from "../../lib/history";

const OUTCOMES: readonly Outcome[] = ["classified", "clarification", "error", "pending"];

export default function UsageCard({
  usage,
  pricingVersion,
}: {
  usage: AdminUsage;
  pricingVersion: string | null;
}) {
  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
          Використання
        </h2>
        {pricingVersion && (
          <span className="text-xs text-slate-400 dark:text-slate-500">
            прайс {pricingVersion}
          </span>
        )}
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <CostTile label="Сьогодні (UTC)" period={usage.today} lead />
        <CostTile label="7 днів" period={usage.week} />
        <CostTile label="Весь час" period={usage.all_time} />
      </div>

      <div className="mt-5 grid gap-5 sm:grid-cols-2">
        <div>
          <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Результати (весь час)
          </h3>
          <ul className="mt-2 space-y-1.5">
            {OUTCOMES.map((outcome) => (
              <li key={outcome} className="flex items-center gap-2 text-sm">
                <OutcomeBadge outcome={outcome} />
                <span className="ml-auto font-medium tabular-nums">
                  {formatInt(usage.all_time.outcomes[outcome] ?? 0)}
                </span>
              </li>
            ))}
          </ul>
        </div>

        <div>
          <h3 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Активні користувачі
          </h3>
          <dl className="mt-2 space-y-1.5 text-sm">
            <Stat label="Сьогодні" value={formatInt(usage.today.users)} hint={split(usage.today)} />
            <Stat label="За 7 днів" value={formatInt(usage.week.users)} hint={split(usage.week)} />
            <Stat
              label="Весь час"
              value={formatInt(usage.all_time.users)}
              hint={split(usage.all_time)}
            />
          </dl>
          <p className="mt-3 text-xs text-slate-500 dark:text-slate-400">
            Рахуються облікові записи, які зробили хоча б одну класифікацію. Запусків за весь
            час: люди {formatInt(usage.all_time.human_runs)} · гості{" "}
            {formatInt(usage.all_time.guest_runs)}.
          </p>
        </div>
      </div>
    </section>
  );
}

/** «люди 3 · гості 41» — the split the author actually reads during a public demo. */
function split(period: UsagePeriod): string {
  return `люди ${formatInt(period.human_users)} · гості ${formatInt(period.guest_users)}`;
}

function CostTile({
  label,
  period,
  lead = false,
}: {
  label: string;
  period: UsagePeriod;
  lead?: boolean;
}) {
  const unpriced = period.runs_unpriced ?? 0;

  return (
    <div
      // The exact lower bound the server aggregated from, verbatim. "Сьогодні" is a label;
      // this is the fact behind it.
      title={period.since ? `з ${period.since}` : "за весь час"}
      className={[
        "rounded-lg border p-4",
        lead
          ? "border-slate-300 bg-slate-50 dark:border-slate-700 dark:bg-slate-950/60"
          : "border-slate-200 dark:border-slate-800",
      ].join(" ")}
    >
      <p className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p className="mt-1 text-3xl font-semibold tracking-tight tabular-nums">
        {formatUsd(period.cost_usd)}
      </p>
      <p className="mt-1 text-sm text-slate-600 dark:text-slate-300 tabular-nums">
        {formatInt(period.runs)} запусків
      </p>
      <p className="mt-2 text-xs text-slate-500 dark:text-slate-400 tabular-nums">
        вхід {formatTokens(period.tokens_in)} · кеш {formatTokens(period.tokens_cached)} · вихід{" "}
        {formatTokens(period.tokens_out)}
      </p>
      {unpriced > 0 && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          {formatInt(unpriced)} без ціни — модель поза прайсом, у суму не входять
        </p>
      )}
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <div className="flex items-baseline gap-2">
        <dt>{label}</dt>
        <dd className="ml-auto font-medium tabular-nums">{value}</dd>
      </div>
      {hint && (
        <p className="text-xs text-slate-500 dark:text-slate-400 tabular-nums">{hint}</p>
      )}
    </div>
  );
}
