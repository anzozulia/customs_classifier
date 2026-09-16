/**
 * The audit view for one classification: what was asked, what came back, and every tool call
 * that produced it — plus the model id, prompt version and dataset hash the answer was
 * produced under, so the row stays interpretable after any of the three change.
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { CodeList, OutcomeBadge } from "../components/HistoryParts";
import type { HistoryDetail } from "../lib/history";
import { fetchHistoryEntry, formatDateTime, formatDuration } from "../lib/history";

export default function HistoryDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [entry, setEntry] = useState<HistoryDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setEntry(null);
    setError(null);

    fetchHistoryEntry(id)
      .then((detail) => {
        if (!cancelled) setEntry(detail);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Не вдалося завантажити");
      });

    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <div className="mx-auto h-full w-full max-w-5xl overflow-y-auto px-4 py-4">
      <Link className="text-sm underline underline-offset-2" to="/history">
        ← до історії
      </Link>

      {error && (
        <p role="alert" className="mt-6 text-sm text-rose-600 dark:text-rose-400">
          {error}
        </p>
      )}

      {!entry && !error && (
        <p className="mt-6 text-sm text-slate-500 dark:text-slate-400">Завантаження…</p>
      )}

      {entry && (
        <>
          <div className="card mt-4 p-4">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
              <span>{formatDateTime(entry.created_at)}</span>
              <OutcomeBadge outcome={entry.outcome} />
              <span>{formatDuration(entry.duration_ms)}</span>
              {entry.thread_id && (
                <Link
                  className="ml-auto underline underline-offset-2"
                  to={`/?thread=${encodeURIComponent(entry.thread_id)}`}
                >
                  відкрити чат
                </Link>
              )}
            </div>

            <p className="mt-3 text-base font-medium">{entry.input_text}</p>

            {entry.codes && entry.codes.length > 0 && (
              <div className="mt-4">
                <CodeList codes={entry.codes} />
              </div>
            )}

            {entry.clarification_question && (
              <p className="mt-4 text-sm text-amber-700 dark:text-amber-300">
                Уточнення: {entry.clarification_question}
              </p>
            )}

            {entry.error_class && (
              <p className="mt-4 font-mono text-sm text-rose-600 dark:text-rose-400">
                {entry.error_class}
              </p>
            )}
          </div>

          <section className="mt-4">
            <h2 className="text-sm font-semibold">Параметри виконання</h2>
            <dl className="card mt-2 grid grid-cols-1 gap-x-6 gap-y-2 p-4 text-sm sm:grid-cols-2">
              <Meta label="Модель" value={entry.model} mono />
              <Meta label="Версія промпту" value={entry.prompt_version} mono />
              <Meta label="Хеш довідника" value={shorten(entry.dataset_sha256)} mono />
              <Meta label="Токени (вхід / вихід)" value={formatTokens(entry)} />
              <Meta label="Вартість" value={formatCost(entry.cost_usd)} />
              <Meta label="Ідентифікатор" value={entry.id} mono />
            </dl>
          </section>

          <section className="mt-4">
            <h2 className="text-sm font-semibold">
              Виклики інструментів ({entry.tool_calls?.length ?? 0})
            </h2>
            {entry.tool_calls && entry.tool_calls.length > 0 ? (
              <ol className="mt-2 space-y-2">
                {entry.tool_calls.map((call, index) => (
                  <li key={`${call.name}-${index}`} className="card p-3">
                    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                      <span className="text-xs text-slate-400">{index + 1}</span>
                      <span className="font-mono text-sm">{call.name}</span>
                      {call.ok === false && (
                        <span className="text-xs text-rose-600 dark:text-rose-400">помилка</span>
                      )}
                      <span className="ml-auto text-xs text-slate-500 dark:text-slate-400">
                        {formatDuration(call.duration_ms)}
                      </span>
                    </div>

                    {call.arguments && Object.keys(call.arguments).length > 0 && (
                      <pre className="mt-2 overflow-x-auto rounded-md bg-slate-100 p-2 font-mono text-xs dark:bg-slate-800">
                        {JSON.stringify(call.arguments)}
                      </pre>
                    )}

                    {call.summary && (
                      <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
                        {call.summary}
                      </p>
                    )}

                    {call.error && (
                      <p className="mt-2 font-mono text-xs text-rose-600 dark:text-rose-400">
                        {call.error}
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                Інструменти не викликалися.
              </p>
            )}
          </section>
        </>
      )}
    </div>
  );
}

function Meta({ label, value, mono }: { label: string; value?: string | null; mono?: boolean }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2">
      <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className={mono ? "font-mono text-xs" : undefined}>{value ?? "—"}</dd>
    </div>
  );
}

function shorten(hash?: string | null): string | null {
  return hash ? `${hash.slice(0, 12)}…` : null;
}

function formatTokens(entry: HistoryDetail): string | null {
  if (entry.tokens_in == null && entry.tokens_out == null) return null;
  return `${entry.tokens_in ?? 0} / ${entry.tokens_out ?? 0}`;
}

function formatCost(value?: number | null): string | null {
  return typeof value === "number" ? `$${value.toFixed(4)}` : null;
}
