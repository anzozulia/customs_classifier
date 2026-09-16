/**
 * Runtime configuration, fetched from the server on mount.
 *
 * NOT `import.meta.env`. Vite inlines `VITE_*` into the bundle at build time, so reading the
 * ChatKit domain key from there would mean rebuilding the JS to change the domain it is
 * registered for. The key is public — it ships in the page either way — so the only thing
 * build-time inlining buys is a rebuild step. `GET /api/config` is a few bytes and one
 * request before first paint.
 */

import type { SupportedLocale } from "@openai/chatkit";

export type AppUser = {
  username: string;
};

export type AppConfig = {
  /** `domain_pk_…`. Public. Registered at platform.openai.com → security → domain allowlist. */
  domainKey: string;
  /** 'uk-UA' is a real ChatKit translation, not an English fallback. */
  locale: SupportedLocale;
  /** Always same-origin and always ONE url — the operation is in the POST body, not the path. */
  chatkitUrl: string;
  /** null when there is no session. `/api/config` is public so /login can render. */
  user: AppUser | null;
};

/**
 * The contract with `app/` (documented in web/README.md):
 *
 *   GET /api/config -> 200, always
 *   { "domain_key": "...", "locale": "uk-UA", "chatkit_url": "/chatkit",
 *     "user": { "username": "anton" } | null }
 *
 * camelCase keys are accepted too, so a pydantic alias generator on the other side of the
 * seam is not a bug report.
 */
export async function loadConfig(): Promise<AppConfig> {
  const response = await fetch("/api/config", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`GET /api/config -> ${response.status}`);
  }
  const raw = (await response.json()) as Record<string, unknown>;

  const domainKey = firstString(raw.domain_key, raw.domainKey);
  if (!domainKey) {
    // Without it ChatKit's domain check fails and the frame REMOVES ITSELF from the DOM.
    // Better to say so here than to debug an empty box.
    throw new Error("/api/config did not return domain_key");
  }

  return {
    domainKey,
    locale: toSupportedLocale(firstString(raw.locale)),
    chatkitUrl: firstString(raw.chatkit_url, raw.chatkitUrl) ?? "/chatkit",
    user: toUser(raw.user),
  };
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

function toUser(value: unknown): AppUser | null {
  if (value === null || typeof value !== "object") return null;
  const username = firstString((value as Record<string, unknown>).username);
  return username ? { username } : null;
}

/**
 * `locale` is typed as a 111-member union in the SDK. We support exactly two, so narrow
 * against a list instead of casting a server string straight into the union.
 */
const LOCALES = ["uk-UA", "uk", "en"] as const satisfies readonly SupportedLocale[];

function toSupportedLocale(value: string | null): SupportedLocale {
  const match = LOCALES.find((locale) => locale === value);
  return match ?? "uk-UA";
}
