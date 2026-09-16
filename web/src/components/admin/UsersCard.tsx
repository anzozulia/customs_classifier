/**
 * Users — humans and guests in one table, because they are one table in the database.
 *
 * The design problem here is proportion: in public mode every visitor mints a guest row, so
 * guests will outnumber humans by orders of magnitude within an hour of a demo. They are
 * therefore rendered in a lower register (muted text, a plain badge, no bold username) and
 * filtered by default to nothing — «Усі» is a choice, not the state you land in. Actions
 * that make no sense for a row without a password (set password, grant superuser) are not
 * shown for guests at all, rather than shown and rejected.
 *
 * Every mutation shows the SERVER's message on failure. The 409s here are safety rails with
 * real sentences in them («не можна зняти останнього суперкористувача»), and replacing that
 * with «Помилка 409» would throw away the only useful part.
 */

import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from "react";

import type { AdminUser, UserFilter, UserPatch } from "../../lib/admin";
import { createUser, errorText, fetchUsers, formatDate, formatInt, updateUser } from "../../lib/admin";

const PAGE = 25;

type KindFilter = NonNullable<UserFilter["kind"]>;

const KINDS: readonly { value: KindFilter; label: string }[] = [
  { value: "human", label: "Люди" },
  { value: "guest", label: "Гості" },
  { value: "all", label: "Усі" },
];

export default function UsersCard({ currentUsername }: { currentUsername: string }) {
  const [queryInput, setQueryInput] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [kind, setKind] = useState<KindFilter>("human");

  const [items, setItems] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [busyId, setBusyId] = useState<number | null>(null);
  const [passwordFor, setPasswordFor] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  // Bumped to force a refetch without changing the filter. Part of `reload`'s identity so
  // that "reset the filters AND refetch" is one render and one request, not two.
  const [refreshKey, setRefreshKey] = useState(0);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchUsers({ q: appliedQuery, kind, limit: PAGE, offset: 0 });
      setItems(page.items);
      setTotal(page.total);
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setLoading(false);
    }
  }, [appliedQuery, kind, refreshKey]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const loadMore = async () => {
    setLoadingMore(true);
    setError(null);
    try {
      const page = await fetchUsers({
        q: appliedQuery,
        kind,
        limit: PAGE,
        offset: items.length,
      });
      setItems((current) => [...current, ...page.items]);
      setTotal(page.total);
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setLoadingMore(false);
    }
  };

  const patch = async (user: AdminUser, body: UserPatch) => {
    setBusyId(user.id);
    setError(null);
    try {
      const updated = await updateUser(user.id, body);
      setItems((current) => current.map((row) => (row.id === user.id ? updated : row)));
      setPasswordFor(null);
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setBusyId(null);
    }
  };

  const onSearch = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAppliedQuery(queryInput.trim());
  };

  // `total` is the honest answer; the modulo is the fallback for a server that does not
  // send one — a full page probably has more behind it.
  const hasMore =
    total !== null ? items.length < total : items.length > 0 && items.length % PAGE === 0;

  return (
    <section className="card p-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
          Користувачі
        </h2>
        {total !== null && (
          <span className="text-xs text-slate-400 dark:text-slate-500 tabular-nums">
            показано {formatInt(items.length)} з {formatInt(total)}
          </span>
        )}

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-slate-300 p-0.5 dark:border-slate-700">
            {KINDS.map((item) => (
              <button
                key={item.value}
                type="button"
                onClick={() => setKind(item.value)}
                className={[
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  kind === item.value
                    ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                    : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800",
                ].join(" ")}
              >
                {item.label}
              </button>
            ))}
          </div>

          <form className="flex items-center gap-2" onSubmit={onSearch}>
            <input
              className="field w-40"
              type="search"
              placeholder="ім'я"
              aria-label="Пошук користувача"
              value={queryInput}
              onChange={(event) => setQueryInput(event.target.value)}
            />
            <button type="submit" className="btn-ghost">
              Знайти
            </button>
          </form>

          <button type="button" className="btn-ghost" onClick={() => setCreating((on) => !on)}>
            {creating ? "Закрити" : "Новий"}
          </button>
        </div>
      </div>

      {creating && (
        <CreateUserForm
          onCancel={() => setCreating(false)}
          onCreated={() => {
            // Land on a view where the new account is actually visible.
            setCreating(false);
            setKind("human");
            setAppliedQuery("");
            setQueryInput("");
            setRefreshKey((key) => key + 1);
          }}
        />
      )}

      {error && (
        <p role="alert" className="mt-4 text-sm text-rose-600 dark:text-rose-400">
          {error}
        </p>
      )}

      {loading ? (
        <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">Завантаження…</p>
      ) : items.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500 dark:text-slate-400">Нікого не знайдено.</p>
      ) : (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full min-w-[44rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
              <tr>
                <th className="py-2 pr-3 font-medium">Користувач</th>
                <th className="py-2 pr-3 font-medium">Тип</th>
                <th className="py-2 pr-3 text-right font-medium">Класифікацій</th>
                <th className="py-2 pr-3 font-medium">Остання активність</th>
                <th className="py-2 font-medium">Дії</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {items.map((user) => {
                const guest = user.kind === "guest";
                const busy = busyId === user.id;
                return (
                  <tr
                    key={user.id}
                    className={[
                      guest ? "text-slate-500 dark:text-slate-400" : "",
                      user.is_active ? "" : "opacity-60",
                    ].join(" ")}
                  >
                    <td className="py-2 pr-3 align-top">
                      <span className={guest ? "font-mono text-xs" : "font-medium"}>
                        {user.username}
                      </span>
                      {user.username === currentUsername && (
                        <span className="ml-2 text-xs text-slate-400 dark:text-slate-500">це ви</span>
                      )}
                      {user.is_superuser && (
                        <span
                          className="ml-2 text-xs text-amber-600 dark:text-amber-400"
                          title="Суперкористувач"
                        >
                          ✦
                        </span>
                      )}
                      {!user.is_active && (
                        <span className="ml-2 text-xs text-rose-600 dark:text-rose-400">
                          вимкнено
                        </span>
                      )}
                      {user.display_name && (
                        <span className="block text-xs text-slate-400 dark:text-slate-500">
                          {user.display_name}
                        </span>
                      )}
                      {passwordFor === user.id && (
                        <PasswordForm
                          busy={busy}
                          onCancel={() => setPasswordFor(null)}
                          onSubmit={(password) => void patch(user, { password })}
                        />
                      )}
                    </td>
                    <td className="py-2 pr-3 align-top">
                      <span
                        className={[
                          "rounded-md px-2 py-0.5 text-xs font-medium",
                          guest
                            ? "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400"
                            : "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900",
                        ].join(" ")}
                      >
                        {guest ? "гість" : "людина"}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-right align-top tabular-nums">
                      {formatInt(user.classifications)}
                    </td>
                    <td className="py-2 pr-3 align-top text-xs tabular-nums">
                      {/* A guest never logs in, so `last_seen_at` — written at mint time and
                          then at most once a day by the sliding refresh — is the only
                          "when were they here" they have. Humans keep their login. */}
                      {guest
                        ? formatDate(user.last_seen_at ?? user.created_at)
                        : formatDate(user.last_login_at ?? user.created_at)}
                    </td>
                    <td className="py-2 align-top">
                      <div className="flex flex-wrap gap-1">
                        {!guest && (
                          <RowButton
                            disabled={busy}
                            onClick={() =>
                              setPasswordFor((current) => (current === user.id ? null : user.id))
                            }
                          >
                            пароль
                          </RowButton>
                        )}
                        <RowButton
                          disabled={busy}
                          onClick={() => void patch(user, { is_active: !user.is_active })}
                        >
                          {user.is_active ? "вимкнути" : "увімкнути"}
                        </RowButton>
                        {!guest && (
                          <RowButton
                            disabled={busy}
                            onClick={() => void patch(user, { is_superuser: !user.is_superuser })}
                          >
                            {user.is_superuser ? "зняти ✦" : "зробити ✦"}
                          </RowButton>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {hasMore && (
        <div className="mt-4 flex justify-center">
          <button type="button" className="btn-ghost" onClick={() => void loadMore()} disabled={loadingMore}>
            {loadingMore ? "Завантаження…" : "Показати ще"}
          </button>
        </div>
      )}
    </section>
  );
}

function RowButton({
  children,
  disabled,
  onClick,
}: {
  children: ReactNode;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="rounded-md px-2 py-1 text-xs font-medium text-slate-600 transition-colors
        hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50
        dark:text-slate-300 dark:hover:bg-slate-800"
    >
      {children}
    </button>
  );
}

function PasswordForm({
  busy,
  onCancel,
  onSubmit,
}: {
  busy: boolean;
  onCancel: () => void;
  onSubmit: (password: string) => void;
}) {
  const [password, setPassword] = useState("");

  return (
    <form
      className="mt-2 flex items-center gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (password) onSubmit(password);
      }}
    >
      <input
        className="field w-44"
        type="text"
        autoComplete="new-password"
        placeholder="новий пароль"
        aria-label="Новий пароль"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
      />
      <button type="submit" className="btn-primary" disabled={busy || password.length === 0}>
        {busy ? "…" : "Ок"}
      </button>
      <button type="button" className="btn-ghost" onClick={onCancel}>
        Скасувати
      </button>
    </form>
  );
}

function CreateUserForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [superuser, setSuperuser] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await createUser({
        username: username.trim(),
        password,
        display_name: displayName.trim() || undefined,
        is_superuser: superuser || undefined,
      });
      onCreated();
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="mt-4 rounded-lg border border-slate-200 p-4 dark:border-slate-800"
      onSubmit={submit}
    >
      <div className="grid gap-3 sm:grid-cols-3">
        <div>
          <label htmlFor="new-username" className="mb-1 block text-sm font-medium">
            Користувач
          </label>
          <input
            id="new-username"
            className="field"
            autoComplete="off"
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor="new-password" className="mb-1 block text-sm font-medium">
            Пароль
          </label>
          <input
            id="new-password"
            className="field"
            type="text"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        <div>
          <label htmlFor="new-display-name" className="mb-1 block text-sm font-medium">
            Ім'я <span className="font-normal text-slate-400">(необов'язково)</span>
          </label>
          <input
            id="new-display-name"
            className="field"
            autoComplete="off"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-slate-300 dark:border-slate-700"
            checked={superuser}
            onChange={(event) => setSuperuser(event.target.checked)}
          />
          Суперкористувач
        </label>
        <button type="submit" className="btn-primary" disabled={busy}>
          {busy ? "Створюємо…" : "Створити"}
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel}>
          Скасувати
        </button>
        {error && (
          <p role="alert" className="text-sm text-rose-600 dark:text-rose-400">
            {error}
          </p>
        )}
      </div>
    </form>
  );
}
