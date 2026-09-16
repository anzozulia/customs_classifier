/**
 * Runtime configuration, fetched from the server on mount.
 *
 * NOT `import.meta.env`. Vite inlines `VITE_*` into the bundle at build time, so reading the
 * ChatKit domain key from there would mean rebuilding the JS to change the domain it is
 * registered for. The key is public — it ships in the page either way — so the only thing
 * build-time inlining buys is a rebuild step. `GET /api/config` is a few bytes and one
 * request before first paint.
 *
 * Since the access mode became runtime-switchable this endpoint also carries the answer to
 * "do I need a login screen?". That answer MUST come from here rather than from the bundle:
 * the whole point of the mode switch is that flipping it takes effect on the next request,
 * with no redeploy.
 */

import type { SupportedLocale } from "@openai/chatkit";

/**
 * `public` — anyone who opens the URL is given a guest session and can classify.
 * `private` — a login is required and guest sessions stop resolving immediately.
 */
export type AccessMode = "public" | "private";

/**
 * A guest is a REAL user row, not a parallel identity, so this is the same shape either
 * way. `kind` exists only so the UI can address a guest as «Гість» and hide the affordances
 * that make no sense for one.
 */
export type UserKind = "human" | "guest";

export type AppUser = {
  username: string;
  displayName: string | null;
  kind: UserKind;
  /** Gates the ✦ link to /admin. The server gates the DATA; this only gates the link. */
  isSuperuser: boolean;
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
  /** Defaults to `private` when the server does not say — the closed state is the safe one. */
  accessMode: AccessMode;
};

/**
 * The contract with `app/` (documented in web/README.md):
 *
 *   GET /api/config -> 200, always
 *   { "domain_key": "...", "locale": "uk-UA", "chatkit_url": "/chatkit",
 *     "access_mode": "public" | "private",
 *     "user": { "username": "guest_7f3a1c", "display_name": null,
 *               "kind": "human" | "guest", "is_superuser": false } | null }
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
    accessMode: toAccessMode(firstString(raw.access_mode, raw.accessMode)),
  };
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

function firstBool(...values: unknown[]): boolean {
  return values.some((value) => value === true);
}

function toUser(value: unknown): AppUser | null {
  if (value === null || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const username = firstString(record.username);
  if (!username) return null;
  return {
    username,
    displayName: firstString(record.display_name, record.displayName),
    // Unknown → "human": the guest branch only ever REMOVES affordances (sign-out, the
    // login redirect), so guessing "guest" for a real account would be the harmful default.
    kind: firstString(record.kind) === "guest" ? "guest" : "human",
    isSuperuser: firstBool(record.is_superuser, record.isSuperuser),
  };
}

/** Anything but a literal "public" is private. An unreadable answer must not open the app. */
function toAccessMode(value: string | null): AccessMode {
  return value === "public" ? "public" : "private";
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
