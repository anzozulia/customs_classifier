/**
 * The app shell: header, navigation, theme toggle, sign-out — and the client-side auth
 * guard for every route inside it.
 *
 * This is Tailwind's entire territory. Everything the chat renders is out of reach (see
 * ChatKitPanel.tsx).
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
  { to: "/", label: "Чат", end: true },
  { to: "/history", label: "Історія", end: false },
];

export default function AppShell({ config, scheme, onToggleTheme }: Props) {
  const location = useLocation();

  // The guard. Not security — the server enforces that — but it stops an authenticated-only
  // page from flashing its empty skeleton before the first 401 comes back.
  if (!config.user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`${LOGIN_PATH}?next=${next}`} replace />;
  }

  const signOut = async () => {
    try {
      await apiFetch("/api/logout", { method: "POST" });
    } catch {
      // A failed logout still means the session is unusable to us; go to /login regardless.
    }
    window.location.assign(LOGIN_PATH);
  };

  return (
    <div className="flex h-full flex-col">
      <header className="flex-none border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex w-full max-w-5xl items-center gap-4 px-4 py-3">
          <span className="text-sm font-semibold tracking-tight">Класифікатор УКТЗЕД</span>

          <nav className="flex items-center gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={({ isActive }) =>
                  [
                    "rounded-lg px-3 py-1.5 text-sm transition-colors",
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
            <span className="hidden pr-1 text-sm text-slate-500 dark:text-slate-400 sm:inline">
              {config.user.username}
            </span>
            <button
              type="button"
              onClick={onToggleTheme}
              className="btn-icon"
              aria-label={scheme === "dark" ? "Світла тема" : "Темна тема"}
              title={scheme === "dark" ? "Світла тема" : "Темна тема"}
            >
              {scheme === "dark" ? <SunIcon /> : <MoonIcon />}
            </button>
            <button type="button" onClick={signOut} className="btn-quiet" title="Вийти">
              <SignOutIcon />
              <span className="hidden sm:inline">Вийти</span>
            </button>
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
