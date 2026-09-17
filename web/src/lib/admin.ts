/**
 * The admin seam: the four things the superuser panel can read and change.
 *
 * ── The contract with `app/admin/routes.py` ──────────────────────────────────────────────
 *
 * This file was first written against a contract the server had not shipped yet; what
 * follows is the one the server actually serves, read off `app/admin/routes.py` during the
 * wiring pass. The differences were real and are worth naming, because they are what this
 * module now absorbs so that no component has to:
 *
 *   GET  /api/admin/overview
 *        -> { access_mode, model, reasoning_effort,          // FLAT, not under `settings`
 *             prompt_version, prompt_sha256, dataset_sha256,
 *             usage: { today, last_7d, all_time } }          // `last_7d`, not `week`
 *        Every window carries its OWN outcome mix and its own user counts, and counts turns
 *        as `classifications`, not `runs`.
 *   GET  /api/admin/models    -> a BARE array of { id, selected, …per_mtok, pricing_version }
 *   PUT  /api/admin/settings  -> ONE key per request: { key, value } -> { key, value, source }
 *   GET  /api/admin/users?kind=human|guest&limit=1..1000 -> a BARE array; no `q`, no `offset`
 *   POST /api/admin/users                        -> 201, the created row
 *   POST /api/admin/users/{id}/password|active|superuser -> the updated row
 *
 * 404, not 403, on every one of them for a non-superuser: a 403 confirms the route exists,
 * and the whole point of a hidden panel is that it does not. `apiFetch` would also turn a
 * 403 into a redirect to /login, which is worse than useless for someone already logged in.
 *
 * Snake_case straight off the wire, exactly like `history.ts` — a mapping layer whose only
 * job is renaming is code that can be wrong. The two places this file does more than name
 * things are documented where they happen (`saveSettings`, `fetchUsers`), and the number
 * parsing: a field the server did not send becomes `null` and renders as «—», never as `0`.
 * This dashboard is the author's only spend visibility, and a confident `$0.00` for "I could
 * not read that" is the single worst thing it could print.
 */

import { apiJson } from "./api";
import type { AccessMode, UserKind } from "./config";
import type { Outcome } from "./history";

/**
 * The back-office path. Obscure on purpose: it is linked from nowhere, appears in no nav,
 * and is not guessable from the rest of the URL space. Obscurity is not a control by itself
 * — the control is that every /api/admin route is behind require_superuser — but it keeps
 * the panel out of the way of a public demo audience.
 */
export const ADMIN_PATH = "/backofficeadminpanel";

export type { AccessMode, UserKind };

// ------------------------------------------------------------------------------- settings

export type AdminSettings = {
  access_mode: AccessMode;
  model: string;
  reasoning_effort: string;
};

/** The three the server validates against (`app/runtime_settings.py::ReasoningEffort`). */
export const REASONING_EFFORTS = ["low", "medium", "high"] as const;

/** Write order for a multi-key save; also the key set `saveSettings` is allowed to send. */
const SETTING_KEYS = ["access_mode", "model", "reasoning_effort"] as const;

// ---------------------------------------------------------------------------------- usage

export type UsagePeriod = {
  /** The inclusive lower bound the server actually used, ISO-8601 UTC. Null for all time. */
  since: string | null;
  cost_usd: number | null;
  runs: number | null;
  /**
   * Runs whose model is not in `pricing.py`'s table, so their cost is NULL and is NOT part
   * of `cost_usd`. Printed whenever it is non-zero: the total is a floor, not a guess.
   */
  runs_unpriced: number | null;
  tokens_in: number | null;
  tokens_cached: number | null;
  tokens_out: number | null;
  /** Partial on purpose: an outcome nobody has produced yet may simply be absent. */
  outcomes: Partial<Record<Outcome, number>>;
  /** Distinct users who ran at least one classification in this window, and the split. */
  users: number | null;
  human_users: number | null;
  guest_users: number | null;
  human_runs: number | null;
  guest_runs: number | null;
};

export type AdminUsage = {
  today: UsagePeriod;
  week: UsagePeriod;
  all_time: UsagePeriod;
};

/** Read-only facts about what is actually running right now. */
export type AdminBuild = {
  prompt_version: string | null;
  prompt_sha256: string | null;
  dataset_sha256: string | null;
};

export type AdminOverview = {
  settings: AdminSettings;
  usage: AdminUsage;
  build: AdminBuild;
};

// --------------------------------------------------------------------------------- models

export type AdminModel = {
  id: string;
  /** What the server says is in effect — not what this client believes it selected. */
  selected: boolean;
  /** USD per 1,000,000 tokens. null when the model is not in the price table. */
  input_per_mtok: number | null;
  cached_input_per_mtok: number | null;
  output_per_mtok: number | null;
};

export type AdminModels = {
  models: AdminModel[];
  pricing_version: string | null;
};

// ---------------------------------------------------------------------------------- users

export type AdminUser = {
  id: number;
  username: string;
  display_name: string | null;
  kind: UserKind;
  is_active: boolean;
  is_superuser: boolean;
  created_at: string | null;
  last_login_at: string | null;
  /** Guests never log in, so this is the only "when were they here" they have. */
  last_seen_at: string | null;
  /** How many classifications this row owns. Guests with 0 are the demo's noise floor. */
  classifications: number | null;
};

export type AdminUserPage = {
  items: AdminUser[];
  /** Total matching the filter, for «показано N з M». See `fetchUsers` for what it counts. */
  total: number | null;
};

export type UserFilter = {
  q?: string;
  kind?: "all" | UserKind;
  limit?: number;
  offset?: number;
};

export type NewUser = {
  username: string;
  password: string;
  display_name?: string;
  is_superuser?: boolean;
};

/** Exactly the three things the server has a route for. There is no display_name update. */
export type UserPatch = {
  password?: string;
  is_active?: boolean;
  is_superuser?: boolean;
};

// ------------------------------------------------------------------------------- requests

const JSON_HEADERS = { "Content-Type": "application/json" };

/**
 * The server caps `limit` at 1000 and orders humans first, then guests by most recent
 * activity — so one request is the most relevant page of the table, and `fetchUsers` filters
 * and pages inside it. See there for why that is a deliberate choice and not a shortcut.
 */
const USER_FETCH_LIMIT = 1000;

export async function fetchOverview(): Promise<AdminOverview> {
  return parseOverview(await apiJson<Record<string, unknown>>("/api/admin/overview"));
}

export async function fetchModels(): Promise<AdminModels> {
  return parseModels(await apiJson<unknown>("/api/admin/models"));
}

/**
 * Apply a settings patch, one PUT per key, and return what the server CONFIRMED.
 *
 * `PUT /api/admin/settings` takes a single `{key, value}` and answers with the value it read
 * back out of the database afterwards — so what comes back is a statement about the row, not
 * an echo of the request, and that is what the caller merges into its state.
 *
 * A multi-key save is therefore not atomic: if the model is accepted and the reasoning
 * effort is rejected, the model stays applied and the rejection is thrown with the server's
 * own sentence in it. That is the honest failure for a two-field form — the panel prints the
 * message and «Оновити» re-reads the truth — and a transaction across two runtime settings
 * would be machinery for a case the author will never hit.
 */
export async function saveSettings(patch: Partial<AdminSettings>): Promise<Partial<AdminSettings>> {
  const confirmed: Partial<AdminSettings> = {};

  for (const key of SETTING_KEYS) {
    const value = patch[key];
    if (value === undefined) continue;

    const raw = await apiJson<Record<string, unknown>>("/api/admin/settings", {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({ key, value }),
    });
    const applied = text(raw.value) ?? value;

    if (key === "access_mode") confirmed.access_mode = applied === "public" ? "public" : "private";
    else if (key === "model") confirmed.model = applied;
    else confirmed.reasoning_effort = applied;
  }

  return confirmed;
}

/**
 * One page of users.
 *
 * The server's route takes `kind` and `limit` and nothing else — no `q`, no `offset`, no
 * total — so the search box and the «Показати ще» button are applied here, over the rows it
 * returned. That is a real limit and it is stated rather than hidden: `total` counts matches
 * within the most recent 1000 rows of the chosen kind, not within the whole table. The
 * server's ordering (humans first, then guests by most recent activity) is what makes that
 * window the useful one; a demo that mints more than a thousand guests has a `purge-guests`
 * command for exactly that, and the numbers the author actually watches are on the dashboard
 * above, which is computed by SQL over every row.
 */
export async function fetchUsers(filter: UserFilter = {}): Promise<AdminUserPage> {
  const query = new URLSearchParams();
  if (filter.kind && filter.kind !== "all") query.set("kind", filter.kind);
  query.set("limit", String(USER_FETCH_LIMIT));

  const raw = await apiJson<unknown>(`/api/admin/users?${query.toString()}`);
  const rows = (Array.isArray(raw) ? raw : []).map(parseUser);

  const needle = (filter.q ?? "").trim().toLowerCase();
  const matched = needle
    ? rows.filter(
        (user) =>
          user.username.toLowerCase().includes(needle) ||
          (user.display_name ?? "").toLowerCase().includes(needle),
      )
    : rows;

  const offset = filter.offset ?? 0;
  const limit = filter.limit ?? 25;
  return { items: matched.slice(offset, offset + limit), total: matched.length };
}

export async function createUser(input: NewUser): Promise<AdminUser> {
  const raw = await apiJson<Record<string, unknown>>("/api/admin/users", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(input),
  });
  return parseUser(raw);
}

/**
 * Change one thing about one user.
 *
 * Three narrow POSTs rather than one PATCH, because that is what the server exposes — and
 * the shape is the safer one anyway: each of them carries its own 409 rail (a guest has no
 * password, you cannot demote yourself, you cannot remove the last active superuser), and a
 * single merged request would have to decide what a half-applied patch means.
 */
export async function updateUser(id: number, patch: UserPatch): Promise<AdminUser> {
  const base = `/api/admin/users/${id}`;
  let latest: AdminUser | null = null;

  if (patch.password !== undefined) {
    latest = await postUser(`${base}/password`, { password: patch.password });
  }
  if (patch.is_active !== undefined) {
    latest = await postUser(`${base}/active`, { is_active: patch.is_active });
  }
  if (patch.is_superuser !== undefined) {
    latest = await postUser(`${base}/superuser`, { is_superuser: patch.is_superuser });
  }

  if (latest === null) throw new Error("Нічого не змінено.");
  return latest;
}

async function postUser(path: string, body: Record<string, unknown>): Promise<AdminUser> {
  return parseUser(
    await apiJson<Record<string, unknown>>(path, {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    }),
  );
}

// --------------------------------------------------------------------------------- parsing

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/** A number or nothing. Strings are accepted because NUMERIC(10,6) often arrives as one. */
function num(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function parseSettings(raw: Record<string, unknown>): AdminSettings {
  return {
    // Anything but a literal "public" is private — the same rule `config.ts` applies, and
    // for the same reason: an unreadable answer must never render the app as open.
    access_mode: raw.access_mode === "public" ? "public" : "private",
    model: text(raw.model) ?? "",
    reasoning_effort: text(raw.reasoning_effort) ?? "",
  };
}

function parseOutcomes(value: unknown): Partial<Record<Outcome, number>> {
  const row = record(value);
  const out: Partial<Record<Outcome, number>> = {};
  for (const key of ["classified", "clarification", "error", "pending"] as const) {
    const count = num(row[key]);
    if (count !== null) out[key] = count;
  }
  return out;
}

function parsePeriod(value: unknown): UsagePeriod {
  const row = record(value);
  return {
    since: text(row.since),
    cost_usd: num(row.cost_usd),
    runs: num(row.classifications),
    runs_unpriced: num(row.runs_unpriced),
    tokens_in: num(row.tokens_in),
    tokens_cached: num(row.tokens_cached),
    tokens_out: num(row.tokens_out),
    outcomes: parseOutcomes(row.outcomes),
    users: num(row.users),
    human_users: num(row.human_users),
    guest_users: num(row.guest_users),
    human_runs: num(row.human_classifications),
    guest_runs: num(row.guest_classifications),
  };
}

function parseOverview(raw: Record<string, unknown>): AdminOverview {
  const usage = record(raw.usage);
  return {
    settings: parseSettings(raw),
    usage: {
      today: parsePeriod(usage.today),
      week: parsePeriod(usage.last_7d),
      all_time: parsePeriod(usage.all_time),
    },
    build: {
      prompt_version: text(raw.prompt_version),
      prompt_sha256: text(raw.prompt_sha256),
      dataset_sha256: text(raw.dataset_sha256),
    },
  };
}

function parseModels(raw: unknown): AdminModels {
  const items = Array.isArray(raw) ? raw : [];
  const models = items.map((item): AdminModel => {
    const row = record(item);
    return {
      id: text(row.id) ?? "",
      selected: row.selected === true,
      input_per_mtok: num(row.input_per_mtok),
      cached_input_per_mtok: num(row.cached_input_per_mtok),
      output_per_mtok: num(row.output_per_mtok),
    };
  });
  // The server stamps the price list onto every option; one of them is enough to label the
  // dashboard with, and an empty list simply has nothing to say.
  const version = items.map((item) => text(record(item).pricing_version)).find(Boolean) ?? null;
  return { models, pricing_version: version };
}

function parseUser(value: unknown): AdminUser {
  const row = record(value);
  return {
    id: num(row.id) ?? 0,
    username: text(row.username) ?? "",
    display_name: text(row.display_name),
    kind: row.kind === "guest" ? "guest" : "human",
    is_active: row.is_active !== false,
    is_superuser: row.is_superuser === true,
    created_at: text(row.created_at),
    last_login_at: text(row.last_login_at),
    last_seen_at: text(row.last_seen_at),
    classifications: num(row.classifications),
  };
}

// ------------------------------------------------------------------------------ formatting

const INT = new Intl.NumberFormat("uk-UA", { maximumFractionDigits: 0 });

/**
 * Money, with enough digits to be true. A demo day can easily land between $0.001 and $1,
 * and `$0.00` for three tenths of a cent is a lie the author would only catch on the bill.
 */
export function formatUsd(value: number | null): string {
  if (value === null) return "—";
  if (value === 0) return "$0";
  if (Math.abs(value) < 1) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function formatInt(value: number | null): string {
  return value === null ? "—" : INT.format(value);
}

/** 1 234 567 → «1.2M». Token counts are scale, not accounting. */
export function formatTokens(value: number | null): string {
  if (value === null) return "—";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return INT.format(value);
}

export function shortSha(value: string | null): string {
  return value === null ? "—" : value.slice(0, 12);
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("uk-UA", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

/** Server messages are user-facing (a 409 rail says exactly why); never swallow them. */
export function errorText(cause: unknown): string {
  return cause instanceof Error && cause.message ? cause.message : "Невідома помилка";
}

/** A price per 1M tokens. Always two decimals — `$0.20`, not `$0.2000`. */
export function formatPerMtok(value: number | null): string {
  return value === null ? "—" : `$${value.toFixed(2)}`;
}
