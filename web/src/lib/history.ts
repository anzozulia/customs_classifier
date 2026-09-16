/**
 * The /history register: the surface that makes an answer auditable outside the chat.
 *
 * The field names below mirror the JSON from `app/records/routes.py` verbatim, snake_case
 * included. A camelCase mapping layer over a read-only view would be code whose only job is
 * renaming, so there isn't one. Everything past `id` is optional because the record row is
 * INSERTed pending before the model runs — a row can legitimately have no codes yet.
 */

import { apiJson } from "./api";

export type Outcome = "classified" | "clarification" | "error" | "pending";

export type HistoryCode = {
  code: string;
  description: string;
  /** Ancestors concatenated. Never render a bare `description`: 2,473 leaves read "інші". */
  full_path: string;
  is_primary?: boolean;
};

export type HistoryEntry = {
  id: string;
  created_at: string;
  input_text: string;
  outcome: Outcome;
  duration_ms?: number | null;
  thread_id?: string | null;
  codes?: HistoryCode[];
  clarification_question?: string | null;
  error_class?: string | null;
};

export type ToolCall = {
  name: string;
  arguments?: Record<string, unknown> | null;
  summary?: string | null;
  duration_ms?: number | null;
  ok?: boolean;
  error?: string | null;
};

export type HistoryDetail = HistoryEntry & {
  model?: string | null;
  prompt_version?: string | null;
  dataset_sha256?: string | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  cost_usd?: number | null;
  tool_calls?: ToolCall[];
};

export type HistoryPage = {
  items: HistoryEntry[];
  /** Keyset cursor. Null means this is the last page. */
  next_before?: string | null;
};

export async function fetchHistory(
  options: { q?: string; before?: string; limit?: number } = {},
): Promise<HistoryPage> {
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.before) query.set("before", options.before);
  query.set("limit", String(options.limit ?? 20));
  return apiJson<HistoryPage>(`/api/history?${query.toString()}`);
}

export async function fetchHistoryEntry(id: string): Promise<HistoryDetail> {
  return apiJson<HistoryDetail>(`/api/history/${encodeURIComponent(id)}`);
}

export function exportCsvUrl(q: string): string {
  const query = new URLSearchParams();
  if (q) query.set("q", q);
  const suffix = query.toString();
  return suffix ? `/api/history/export.csv?${suffix}` : "/api/history/export.csv";
}

// ---------------------------------------------------------------------------- formatting

export const OUTCOME_LABEL: Record<Outcome, string> = {
  classified: "класифіковано",
  clarification: "уточнення",
  error: "помилка",
  pending: "виконується",
};

/** UKTZED reads as 4 digits then pairs: 3919101200 -> "3919 10 12 00". */
export function formatCode(code: string): string {
  const digits = code.replace(/\D/g, "");
  if (digits.length <= 4) return digits;
  const tail = digits.slice(4).match(/.{1,2}/g) ?? [];
  return [digits.slice(0, 4), ...tail].join(" ");
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("uk-UA", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDuration(ms: number | null | undefined): string {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "—";
  return ms < 1000 ? `${Math.round(ms)} мс` : `${(ms / 1000).toFixed(1)} с`;
}
