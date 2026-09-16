/**
 * Boot: fetch runtime config, then route.
 *
 * Nothing renders before `GET /api/config` resolves, because the ChatKit domain key comes
 * from there and a frame mounted without one removes itself from the DOM. The alternative —
 * `import.meta.env.VITE_CHATKIT_DOMAIN_KEY`, which is what all four official samples do —
 * is inlined into the bundle by Vite at build time and would make "serve this from a
 * different hostname" a rebuild.
 */

import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import AppShell from "./components/AppShell";
import type { AppConfig } from "./lib/config";
import { loadConfig } from "./lib/config";
import { useColorScheme } from "./lib/theme";
import ChatPage from "./pages/ChatPage";
import HistoryDetailPage from "./pages/HistoryDetailPage";
import HistoryPage from "./pages/HistoryPage";
import LoginPage from "./pages/LoginPage";

export default function App() {
  const { scheme, toggle } = useColorScheme();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    loadConfig()
      .then(setConfig)
      .catch((cause: unknown) => {
        setError(cause instanceof Error ? cause.message : "Невідома помилка");
      });
  }, []);

  useEffect(load, [load]);

  if (error !== null) {
    return (
      <div className="flex h-full items-center justify-center px-4">
        <div className="card max-w-sm p-6 text-center">
          <p className="text-sm font-medium">Не вдалося завантажити конфігурацію</p>
          <p className="mt-2 font-mono text-xs text-slate-500 dark:text-slate-400">{error}</p>
          <button type="button" className="btn-primary mt-4" onClick={load}>
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

  return (
    <Routes>
      <Route path="/login" element={<LoginPage config={config} />} />
      {/* Pathless layout route: the shell holds the auth guard, so every child is guarded. */}
      <Route element={<AppShell config={config} scheme={scheme} onToggleTheme={toggle} />}>
        <Route index element={<ChatPage config={config} scheme={scheme} />} />
        <Route path="history" element={<HistoryPage />} />
        <Route path="history/:id" element={<HistoryDetailPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
