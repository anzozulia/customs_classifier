/**
 * The gate in front of the back-office panel.
 *
 * WHY THIS EXISTS, and what it trades away.
 *
 * The panel used to answer 404 to anyone who was not a superuser — the same answer the server
 * still gives every /api/admin route, and the right answer for a route linked from the UI.
 * But the back office is reached by TYPING its URL, and in public mode every visitor is
 * auto-minted as a guest, so there was no login screen anywhere and the 404 locked the author
 * out of their own kill switch during exactly the demo it exists to end.
 *
 * So the deal changed deliberately: the URL is the secret, and this form is the lock. Anyone
 * who finds `/backofficeadminpanel` learns that something is there — that is the cost — and
 * learns nothing else. In particular this form is IDENTICAL for a stranger, a signed-in
 * ordinary user and a guest: no «у вас немає доступу», no «ви вже увійшли», nothing that
 * says how close you are. The server is unchanged and still 404s every admin API call, so
 * the only thing this page can do without a superuser session is show this form.
 */

import { type FormEvent, useState } from "react";

import { ApiError, apiFetch } from "../../lib/api";

export default function AdminSignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      // Form-encoded, matching app/auth/routes.py. In public mode this REPLACES the caller's
      // guest session with the superuser's — same cookie, new uid — which is what lets the
      // author sign in from a browser that has been browsing the demo as a visitor.
      await apiFetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ username, password }),
      });
      onSignedIn();
    } catch (cause: unknown) {
      // One message for every failure — wrong password, unknown user, disabled account, and
      // "correct credentials but not a superuser" alike. The last case is the important one:
      // a distinct message there would confirm a valid login to someone probing the panel.
      setError(
        cause instanceof ApiError && cause.status >= 500
          ? "Сервіс тимчасово недоступний. Спробуйте пізніше."
          : "Невірне ім'я користувача чи пароль.",
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center px-4">
      <div className="card w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold tracking-tight">Вхід</h1>

        <form className="mt-6 space-y-4" onSubmit={onSubmit}>
          <div>
            <label htmlFor="admin-username" className="mb-1 block text-sm font-medium">
              Користувач
            </label>
            <input
              id="admin-username"
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
            <label htmlFor="admin-password" className="mb-1 block text-sm font-medium">
              Пароль
            </label>
            <input
              id="admin-password"
              name="password"
              type="password"
              className="field"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>

          {error ? (
            <p className="text-sm text-rose-600 dark:text-rose-400" role="alert">
              {error}
            </p>
          ) : null}

          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy ? "Вхід…" : "Увійти"}
          </button>
        </form>
      </div>
    </div>
  );
}
