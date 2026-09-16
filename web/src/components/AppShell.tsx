/**
 * The app shell: header, navigation, theme toggle, sign-out — and the client-side auth
 * guard for every route inside it.
 *
 * This is Tailwind's entire territory. Everything the chat renders is out of reach (see
 * ChatKitPanel.tsx).
 *
 * Two things the header now has to say without becoming a banner:
 *   • a GUEST is told, quietly, that this is a public demo session and that it is theirs;
 *   • a SUPERUSER gets the only link to /admin that exists anywhere in the app. It is a
 *     single ✦ glyph and it is rendered for nobody else — the panel is hidden by the server
 *     answering 404, and the header must not contradict that for the other 99% of visitors.
 */

import { NavLink, Navigate, Outlet, useLocation } from "react-router-dom";

import { LOGIN_PATH, apiFetch } from "../lib/api";
import type { AppConfig } from "../lib/config";
import type { ColorScheme } from "../lib/theme";

type Props = {
  config: AppConfig;
  scheme: ColorScheme;
  onToggleTheme: () => void;
};

const NAV = [
  { to: "/", label: "Класифікатор", end: true },
  { to: "/history", label: "Історія", end: false },
];

export default function AppShell({ config, scheme, onToggleTheme }: Props) {
  const location = useLocation();

  // The guard. Not security — the server enforces that — but it stops an authenticated-only
  // page from flashing its empty skeleton before the first 401 comes back.
  //
  // In public mode this branch is unreachable: App.tsx handles a missing guest before the
  // shell is ever rendered, precisely so that a visitor is never sent to a login screen the
  // author has switched off.
  if (!config.user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`${LOGIN_PATH}?next=${next}`} replace />;
  }

  const user = config.user;
  const isGuest = user.kind === "guest";

  const signOut = async () => {
    try {
      await apiFetch("/api/logout", { method: "POST" });
    } catch {
      // A failed logout still means the session is unusable to us; go to /login regardless.
    }
    window.location.assign(LOGIN_PATH);
  };

  // A guest has nothing to sign out OF — «Вийти» would drop them onto a login screen they
  // have no password for. The same call is still the useful one, relabelled for what it
  // actually does for them: drop this guest identity so the next request mints a fresh one
  // with an empty history. Destructive enough to be worth a confirm.
  const startOver = async () => {
    const ok = window.confirm(
      "Почати заново? Поточна демо-сесія та її історія стануть недоступними.",
    );
    if (!ok) return;
    try {
      await apiFetch("/api/logout", { method: "POST" });
    } catch {
      // Same as above: whatever the server said, we want a clean slate on the next load.
    }
    window.location.assign("/");
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex-none border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex w-full max-w-5xl items-center gap-2 px-4 py-3 sm:gap-4">
          {/* The wordmark and the first nav item now share a word, so they are separated by
              WEIGHT rather than by wording: «Класифікатор» is de-emphasised here so the eye
              lands on УКТЗЕД as the product name, while the nav below is uppercase and
              letter-spaced — a different typographic register, read as chrome, not a title.

              Below `sm` the shared word is dropped entirely rather than shrunk: the nav pill
              beside it already says КЛАСИФІКАТОР, and the row now has to fit a demo marker
              too. A phone is the likeliest way a public visitor arrives, so the marker wins
              the space over a word printed twice. */}
          <span className="flex items-baseline gap-1.5 text-sm">
            <span className="hidden font-normal text-slate-400 dark:text-slate-500 sm:inline">
              Класифікатор
            </span>
            <span className="font-semibold tracking-tight">УКТЗЕД</span>
          </span>

          <span
            aria-hidden="true"
            className="hidden h-5 w-px flex-none bg-slate-200 sm:block dark:bg-slate-800"
          />

          <nav className="flex min-w-0 items-center gap-0.5 sm:gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    "rounded-lg px-2 py-1.5 text-xs font-medium uppercase tracking-wider sm:px-3",
                    "whitespace-nowrap transition-colors",
                    isActive
                      ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
                  ].join(" ")
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>

          <div className="ml-auto flex items-center gap-1">
            {isGuest ? (
              <span
                className="flex items-center gap-2 pr-1"
                title="Публічна демо-версія. Ваша історія збережеться у цьому браузері."
              >
                <span className="rounded-full border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400">
                  демо
                </span>
                <span className="hidden text-sm text-slate-500 dark:text-slate-400 sm:inline">
                  Гість
                </span>
              </span>
            ) : (
              <span className="hidden pr-1 text-sm text-slate-500 dark:text-slate-400 sm:inline">
                {user.displayName ?? user.username}
              </span>
            )}

            {user.isSuperuser && (
              <NavLink
                to="/admin"
                className={({ isActive }) =>
                  [
                    "btn-icon text-base leading-none",
                    isActive ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100" : "",
                  ].join(" ")
                }
                aria-label="Адміністрування"
                title="Адміністрування"
              >
                ✦
              </NavLink>
            )}

            <button
              type="button"
              onClick={onToggleTheme}
              className="btn-icon"
              aria-label={scheme === "dark" ? "Світла тема" : "Темна тема"}
              title={scheme === "dark" ? "Світла тема" : "Темна тема"}
            >
              {scheme === "dark" ? <SunIcon /> : <MoonIcon />}
            </button>

            {isGuest ? (
              <button
                type="button"
                onClick={() => void startOver()}
                className="btn-quiet"
                title="Почати заново з порожньою історією"
              >
                <RefreshIcon />
                <span className="hidden sm:inline">Почати заново</span>
              </button>
            ) : (
              <button type="button" onClick={() => void signOut()} className="btn-quiet" title="Вийти">
                <SignOutIcon />
                <span className="hidden sm:inline">Вийти</span>
              </button>
            )}
          </div>
        </div>
      </header>

      {/* min-h-0 is load-bearing: without it a flex child refuses to shrink and the chat,
          which is height:100%, overflows the viewport instead of scrolling internally. */}
      <main className="min-h-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 12a9 9 0 0 1 15.5-6.2L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-15.5 6.2L3 16" />
      <path d="M3 21v-5h5" />
    </svg>
  );
}

function SignOutIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="m16 17 5-5-5-5" />
      <path d="M21 12H9" />
    </svg>
  );
}
