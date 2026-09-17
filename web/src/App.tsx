/**
 * Boot: fetch runtime config, then route.
 *
 * Nothing renders before `GET /api/config` resolves, because the ChatKit domain key comes
 * from there and a frame mounted without one removes itself from the DOM. The alternative —
 * `import.meta.env.VITE_CHATKIT_DOMAIN_KEY`, which is what all four official samples do —
 * is inlined into the bundle by Vite at build time and would make "serve this from a
 * different hostname" a rebuild.
 *
 * The same request now also decides whether a login screen exists at all. In `public` mode
 * the server mints a guest user, so the guard in AppShell passes and the visitor lands in
 * the classifier; in `private` mode nothing about the old behaviour changes.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Route, Routes } from "react-router-dom";

import AppShell from "./components/AppShell";
import { ADMIN_PATH } from "./lib/admin";
import type { AppConfig } from "./lib/config";
import { loadConfig } from "./lib/config";
import { useColorScheme } from "./lib/theme";
import AdminPage from "./pages/AdminPage";
import ChatPage from "./pages/ChatPage";
import HistoryDetailPage from "./pages/HistoryDetailPage";
import HistoryPage from "./pages/HistoryPage";
import LoginPage from "./pages/LoginPage";
import NotFoundPage from "./pages/NotFoundPage";

export default function App() {
  const { scheme, toggle } = useColorScheme();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (): Promise<void> => {
    setError(null);
    try {
      setConfig(await loadConfig());
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : "Невідома помилка");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error !== null) {
    return (
      <div className="flex h-full items-center justify-center px-4">
        <div className="card max-w-sm p-6 text-center">
          <p className="text-sm font-medium">Не вдалося завантажити конфігурацію</p>
          <p className="mt-2 font-mono text-xs text-slate-500 dark:text-slate-400">{error}</p>
          <button type="button" className="btn-primary mt-4" onClick={() => void load()}>
            Спробувати ще раз
          </button>
        </div>
      </div>
    );
  }

  if (config === null) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-slate-500 dark:text-slate-400">Завантаження…</p>
      </div>
    );
  }

  // In public mode `/api/config` mints a guest, so `user` should never be null here. If it
  // is — a first-ever visit whose Set-Cookie has not landed, or a browser refusing cookies —
  // the ONE thing we must not do is what the old guard did and send them to /login: in
  // public mode that is a screen with no password behind it, and the redirect would repeat
  // on every load. So: retry once, then say plainly what happened. /login stays routable
  // throughout, because the author still has to be able to sign in and close the demo.
  const awaitingGuest = config.accessMode === "public" && config.user === null;

  return (
    <Routes>
      <Route path="/login" element={<LoginPage config={config} />} />
      {/* OUTSIDE the shell and outside the guest gate on purpose. The back office must be
          reachable while the app is public (where nobody is asked to log in) and while a
          guest session is still settling — otherwise the author cannot get in to close the
          very demo this route exists to end. It renders its own sign-in form. */}
      <Route path={ADMIN_PATH} element={<AdminPage config={config} />} />
      {awaitingGuest ? (
        <Route path="*" element={<GuestSessionPending onRetry={load} />} />
      ) : (
        /* Pathless layout route: the shell holds the auth guard, so every child is guarded. */
        <Route element={<AppShell config={config} scheme={scheme} onToggleTheme={toggle} />}>
          <Route index element={<ChatPage config={config} scheme={scheme} />} />
          <Route path="history" element={<HistoryPage />} />
          <Route path="history/:id" element={<HistoryDetailPage />} />
          {/* Inside the layout on purpose: an unknown URL and a forbidden /admin have to
              look identical, header included. */}
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      )}
    </Routes>
  );
}

/**
 * Exactly one automatic retry (the ref survives StrictMode's double-invoked effect), then a
 * manual button. Never an automatic reload loop.
 */
function GuestSessionPending({ onRetry }: { onRetry: () => Promise<void> }) {
  const [busy, setBusy] = useState(true);
  const started = useRef(false);

  const retry = useCallback(() => {
    setBusy(true);
    void onRetry().finally(() => setBusy(false));
  }, [onRetry]);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    retry();
  }, [retry]);

  return (
    <div className="flex h-full items-center justify-center px-4">
      <div className="card max-w-sm p-6 text-center">
        {busy ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">Готуємо демо-сесію…</p>
        ) : (
          <>
            <p className="text-sm font-medium">Не вдалося створити демо-сесію</p>
            <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
              Найімовірніше браузер блокує файли cookie для цього сайту.
            </p>
            <button type="button" className="btn-primary mt-4" onClick={retry}>
              Спробувати ще раз
            </button>
          </>
        )}
      </div>
    </div>
  );
}
