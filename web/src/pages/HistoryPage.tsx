/**
 * The register of codes — the surface ChatKit's own thread sidebar cannot be.
 *
 * ChatKit's history panel lists CONVERSATIONS. This lists CLASSIFICATIONS: the input, the
 * codes, the navigation path and, deliberately, the failures — a turn that errored is a row
 * here rather than something that quietly never happened.
 */

import { type FormEvent, useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { CodeList, OutcomeBadge } from "../components/HistoryParts";
import type { HistoryEntry } from "../lib/history";
import { exportCsvUrl, fetchHistory, formatDateTime, formatDuration } from "../lib/history";

export default function HistoryPage() {
  const [queryInput, setQueryInput] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetchHistory({ q: appliedQuery })
      .then((page) => {
        if (cancelled) return;
        setEntries(page.items);
        setCursor(page.next_before ?? null);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "Не вдалося завантажити");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [appliedQuery]);

  const loadMore = useCallback(async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      // Keyset, not OFFSET: with rows arriving while you page, OFFSET both degrades and
      // shows the same row twice.
      const page = await fetchHistory({ q: appliedQuery, before: cursor });
      setEntries((current) => [...current, ...page.items]);
      setCursor(page.next_before ?? null);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "Не вдалося завантажити");
    } finally {
      setLoadingMore(false);
    }
  }, [appliedQuery, cursor]);

  const onSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAppliedQuery(queryInput.trim());
  };

  return (
    <div className="mx-auto h-full w-full max-w-5xl overflow-y-auto px-4 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Історія класифікацій</h1>
        <form className="ml-auto flex items-center gap-2" onSubmit={onSearch}>
          <input
            className="field w-56"
            type="search"
            placeholder="пошук: плівка ПВХ"
            aria-label="Пошук в історії"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
          />
          <button type="submit" className="btn-ghost">
            Знайти
          </button>
          <a className="btn-ghost" href={exportCsvUrl(appliedQuery)} download>
            CSV
          </a>
        </form>
      </div>

      {error && (
        <p role="alert" className="mt-6 text-sm text-rose-600 dark:text-rose-400">
          {error}
        </p>
      )}

      {loading ? (
        <p className="mt-6 text-sm text-slate-500 dark:text-slate-400">Завантаження…</p>
      ) : entries.length === 0 ? (
        <p className="mt-6 text-sm text-slate-500 dark:text-slate-400">
          {appliedQuery ? "Нічого не знайдено." : "Тут з'являться ваші класифікації."}
        </p>
      ) : (
        <ul className="mt-4 space-y-3">
          {entries.map((entry) => (
            <li key={entry.id} className="card p-4">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
                <span>{formatDateTime(entry.created_at)}</span>
                <OutcomeBadge outcome={entry.outcome} />
                <span>{formatDuration(entry.duration_ms)}</span>
                <span className="ml-auto flex items-center gap-3">
                  <Link className="underline underline-offset-2" to={`/history/${entry.id}`}>
                    деталі
                  </Link>
                  {entry.thread_id && (
                    <Link
                      className="underline underline-offset-2"
                      to={`/?thread=${encodeURIComponent(entry.thread_id)}`}
                    >
                      відкрити чат
                    </Link>
                  )}
                </span>
              </div>

              <p className="mt-2 text-sm font-medium">{entry.input_text}</p>

              {entry.codes && entry.codes.length > 0 && (
                <div className="mt-3">
                  <CodeList codes={entry.codes} />
                </div>
              )}

              {entry.clarification_question && (
                <p className="mt-3 text-sm text-amber-700 dark:text-amber-300">
                  Уточнення: {entry.clarification_question}
                </p>
              )}

              {entry.error_class && (
                <p className="mt-3 font-mono text-sm text-rose-600 dark:text-rose-400">
                  {entry.error_class}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}

      {cursor && (
        <div className="mt-4 flex justify-center">
          <button type="button" className="btn-ghost" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? "Завантаження…" : "Показати ще"}
          </button>
        </div>
      )}
    </div>
  );
}
