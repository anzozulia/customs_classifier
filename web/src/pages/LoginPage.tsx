import { type FormEvent, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";

import type { AppConfig } from "../lib/config";

/** Open-redirect guard: only same-origin, path-relative destinations. */
function safeNext(raw: string | null): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return "/";
  return raw;
}

export default function LoginPage({ config }: { config: AppConfig }) {
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // A GUEST must not be bounced off this page. In public mode every visitor has a user row,
  // the author included — and the author still has to be able to sign in HERE to reach the
  // panel and close the demo. So only a real account counts as "already signed in".
  if (config.user && config.user.kind === "human") {
    return <Navigate to={next} replace />;
  }

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      // Plain fetch, not apiFetch: apiFetch's 401 handler redirects to /login, which from
      // /login is a loop. A bad password is an expected answer here, not a dead session.
      //
      // Form-encoded, because the server side is written as FastAPI `Form(...)` (which is
      // why python-multipart is pinned). URLSearchParams makes fetch set
      // `Content-Type: application/x-www-form-urlencoded` by itself. If the endpoint ever
      // takes a pydantic body instead, this becomes JSON.stringify + an explicit header.
      const response = await fetch("/api/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        body: new URLSearchParams({ username, password }),
      });

      // `ok` covers both shapes the server may use: a 204, or a 303 to "/" that fetch
      // follows transparently into a 200.
      if (!response.ok) {
        setError(
          response.status === 401
            ? "Невірне ім'я користувача або пароль."
            : `Не вдалося увійти (помилка ${response.status}).`,
        );
        return;
      }

      // Hard navigation, not a router push: the session cookie is new, so /api/config has
      // to be re-fetched to learn who we are. A reload is the whole of that logic.
      window.location.assign(next);
    } catch {
      setError("Сервер недоступний. Спробуйте ще раз.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center px-4">
      <div className="card w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold tracking-tight">Класифікатор УКТЗЕД</h1>

        {config.accessMode === "public" && (
          <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
            Зараз відкрито публічний доступ — щоб користуватися класифікатором, входити не
            потрібно.{" "}
            <Link className="underline underline-offset-2" to="/">
              Повернутися до класифікатора
            </Link>
            .
          </p>
        )}

        <form className="mt-6 space-y-4" onSubmit={onSubmit}>
          <div>
            <label htmlFor="username" className="mb-1 block text-sm font-medium">
              Користувач
            </label>
            <input
              id="username"
              name="username"
              className="field"
              autoComplete="username"
              autoFocus
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>

          <div>
            <label htmlFor="password" className="mb-1 block text-sm font-medium">
              Пароль
            </label>
            <input
              id="password"
              name="password"
              type="password"
              className="field"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>

          {error && (
            <p role="alert" className="text-sm text-rose-600 dark:text-rose-400">
              {error}
            </p>
          )}

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? "Входимо…" : "Увійти"}
          </button>
        </form>
      </div>
    </div>
  );
}
