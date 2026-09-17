/**
 * The app shell: header, navigation, theme toggle, sign-out — and the client-side auth
 * guard for every route inside it.
 *
 * This is Tailwind's entire territory. Everything the chat renders is out of reach (see
 * ChatKitPanel.tsx).
 *
 * Two things the header has to say without becoming a banner:
 *   • a GUEST is told, quietly, that this is a public demo session and that it is theirs;
 *   • a SUPERUSER gets the only link to /admin that exists anywhere in the app. It is a
 *     single ✦ glyph and it is rendered for nobody else — the panel is hidden by the server
 *     answering 404, and the header must not contradict that for the other 99% of visitors.
 *
 * ── Why the header is rebuilt, not reflowed, below `md` ───────────────────────────────
 *
 * In one row at phone width the header needed ~414px: the УКТЗЕД wordmark, the
 * КЛАСИФІКАТОР pill, ІСТОРІЯ, the «демо» badge a guest carries, and two icon buttons. On a
 * 412px Galaxy S24+ that overflowed, and since every label is `whitespace-nowrap` there was
 * nothing to shrink — so `ml-auto` drove the right-hand cluster straight across ІСТОРІЯ.
 *
 * Letting the row wrap removed the overlap but answered the wrong question: a two-line
 * header on a phone is a worse header, not a fixed one. So below `md` the row is rebuilt:
 *
 *   • the wordmark goes. The nav pill beside it already reads КЛАСИФІКАТОР, so it was the
 *     one element paying ~60px to repeat a word that was already on screen.
 *   • everything on the right collapses into one burger — 129px of badge and glyphs for a
 *     36px button — and the controls come back as LABELLED rows. «Почати заново» was a bare
 *     ⟳ on a phone, which is not a thing anyone can be expected to read.
 *
 * What is left is nav + burger ≈ 259px at 320px wide: one row on any phone, with no width
 * at which two elements reach for the same pixels. The «демо» marker moves into the menu; a
 * guest still meets the standing «Довідкова класифікація» disclaimer under the composer.
 */

import { useEffect, useRef, useState } from "react";
import { NavLink, Navigate, Outlet, useLocation } from "react-router-dom";

import { ADMIN_PATH } from "../lib/admin";
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

/** Tailwind's `md`, NOT `sm`. The inline cluster needs ~700px — wordmark, nav, display
 *  name, ✦, theme and «Вийти» — so switching to it at `sm` (640px) overflowed the page
 *  sideways by 56px in the 640..696 band. Named because the menu's open/closed state
 *  keys off the same breakpoint. */
const MENU_BREAKPOINT = "(min-width: 768px)";

export default function AppShell({ config, scheme, onToggleTheme }: Props) {
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  // Every hook runs before the guard below returns: the redirect is conditional, the hooks
  // must not be.

  // A menu that survived the navigation it triggered would cover the page you just asked for.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    // `pointerdown`, not `click`: the menu must be gone before whatever sits under it
    // reacts. The burger itself is inside `menuRef`, so its own toggle still works.
    const onPointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    // Above the breakpoint these same controls render inline, so a menu left open across it
    // (rotate a phone, split-screen a tablet) would show every one of them twice.
    const wide = window.matchMedia(MENU_BREAKPOINT);
    const onBreakpoint = () => {
      if (wide.matches) setMenuOpen(false);
    };

    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    wide.addEventListener("change", onBreakpoint);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
      wide.removeEventListener("change", onBreakpoint);
    };
  }, [menuOpen]);

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

  const themeLabel = scheme === "dark" ? "Світла тема" : "Темна тема";
  const ThemeIcon = scheme === "dark" ? SunIcon : MoonIcon;
  const endSessionLabel = isGuest ? "Почати заново" : "Вийти";
  const EndSessionIcon = isGuest ? RefreshIcon : SignOutIcon;
  const endSession = () => void (isGuest ? startOver() : signOut());

  return (
    <div className="flex h-full flex-col">
      {/* `relative` anchors the mobile menu; the z-index keeps it above the chat, which
          mounts positioned content of its own. */}
      <header className="relative z-40 flex-none border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex w-full max-w-5xl items-center gap-2 px-4 py-3 md:gap-4">
          {/* The wordmark and the first nav item share a word, so they are separated by
              WEIGHT rather than by wording: «Класифікатор» is de-emphasised here so the eye
              lands on УКТЗЕД as the product name, while the nav is uppercase and
              letter-spaced — a different typographic register, read as chrome, not a title.
              Below `md` the whole thing goes; see the note at the top of the file. */}
          <span className="hidden items-baseline gap-1.5 text-sm md:flex">
            <span className="font-normal text-slate-400 dark:text-slate-500">Класифікатор</span>
            <span className="font-semibold tracking-tight">УКТЗЕД</span>
          </span>

          <span
            aria-hidden="true"
            className="hidden h-5 w-px flex-none bg-slate-200 md:block dark:bg-slate-800"
          />

          {/* `flex-none`, because these labels are `whitespace-nowrap`: a nav allowed to
              shrink clips its own text rather than reflowing it. */}
          <nav className="flex flex-none items-center gap-0.5 sm:gap-1">
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

          {/* ── sm and up: the controls inline, where there is room for them ───────────── */}
          <div className="ml-auto hidden min-w-0 items-center gap-1 md:flex">
            {isGuest ? (
              <span
                className="flex min-w-0 items-center gap-2 pr-1"
                title="Публічна демо-версія. Ваша історія збережеться у цьому браузері."
              >
                <span className="flex-none rounded-full border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400">
                  демо
                </span>
                <span className="truncate text-sm text-slate-500 dark:text-slate-400">Гість</span>
              </span>
            ) : (
              <span className="truncate pr-1 text-sm text-slate-500 dark:text-slate-400">
                {user.displayName ?? user.username}
              </span>
            )}

            {user.isSuperuser && (
              <NavLink
                to={ADMIN_PATH}
                className={({ isActive }) =>
                  [
                    "btn-icon text-base leading-none",
                    isActive
                      ? "bg-slate-100 text-slate-900 dark:bg-slate-800 dark:text-slate-100"
                      : "",
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
              aria-label={themeLabel}
              title={themeLabel}
            >
              <ThemeIcon />
            </button>

            <button
              type="button"
              onClick={endSession}
              className="btn-quiet"
              title={isGuest ? "Почати заново з порожньою історією" : "Вийти"}
            >
              <EndSessionIcon />
              <span>{endSessionLabel}</span>
            </button>
          </div>

          {/* ── below sm: one button for all of the above ──────────────────────────────── */}
          <div className="relative ml-auto flex-none md:hidden" ref={menuRef}>
            <button
              type="button"
              onClick={() => setMenuOpen((open) => !open)}
              className="btn-icon"
              aria-label={menuOpen ? "Закрити меню" : "Меню"}
              aria-expanded={menuOpen}
              aria-controls="app-menu"
              aria-haspopup="menu"
            >
              {menuOpen ? <CloseIcon /> : <MenuIcon />}
            </button>

            {menuOpen && (
              <div
                id="app-menu"
                role="menu"
                className="absolute right-0 top-full z-50 mt-2 w-60 rounded-xl border border-slate-200 bg-white p-1.5 shadow-lg dark:border-slate-700 dark:bg-slate-900"
              >
                {/* Who you are. For a guest this is the only place the «демо» marker still
                    appears on a phone, so it keeps the badge, not just the word. */}
                <div className="flex items-center gap-2 px-3 py-2">
                  {isGuest && (
                    <span className="flex-none rounded-full border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400">
                      демо
                    </span>
                  )}
                  <span className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {isGuest ? "Гість" : (user.displayName ?? user.username)}
                  </span>
                </div>

                {isGuest && (
                  <p className="px-3 pb-2 text-xs leading-snug text-slate-400 dark:text-slate-500">
                    Публічна демо-версія. Ваша історія збережеться у цьому браузері.
                  </p>
                )}

                <div className="my-1 h-px bg-slate-200 dark:bg-slate-800" />

                {user.isSuperuser && (
                  <NavLink to={ADMIN_PATH} role="menuitem" className="menu-row">
                    <span aria-hidden="true" className="w-4 flex-none text-center text-base leading-none">
                      ✦
                    </span>
                    Адміністрування
                  </NavLink>
                )}

                {/* Deliberately does NOT close the menu: it is the one control here you
                    might flip twice, and leaving it open makes the second tap trivial. */}
                <button type="button" role="menuitem" onClick={onToggleTheme} className="menu-row">
                  <ThemeIcon />
                  {themeLabel}
                </button>

                <button
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setMenuOpen(false);
                    endSession();
                  }}
                  className="menu-row"
                >
                  <EndSessionIcon />
                  {endSessionLabel}
                </button>
              </div>
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
    <svg viewBox="0 0 24 24" className="h-4 w-4 flex-none" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4 flex-none" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
    </svg>
  );
}

function MenuIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 flex-none" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" className="h-5 w-5 flex-none" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4 flex-none"
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
      className="h-4 w-4 flex-none"
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
