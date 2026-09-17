/**
 * The hidden admin panel. Reached by typing /admin; linked from nowhere except the ✦ in the
 * header, which only a superuser is shown.
 *
 * Reached by TYPING its obscure URL (see ADMIN_PATH); linked from nowhere except the ✦ in
 * the header, which only a superuser is shown.
 *
 * When the server answers 404 to `GET /api/admin/overview` — which it does for a stranger, an
 * ordinary user and a guest alike — this page shows a SIGN-IN FORM, not a not-found card.
 * That is a deliberate change from the original design: the back office is reached by typing
 * a URL, and in public mode every visitor is auto-minted as a guest, so a 404 here left the
 * author with no way to sign in and close their own demo. The URL is the secret; the form is
 * the lock. The form itself discloses nothing — it is identical whoever is looking at it.
 *
 * Sections rather than tabs. The access-mode switch is the author's kill switch during a
 * live demo and the usage numbers are their only spend visibility, so neither may be one
 * click away behind a tab — both are on screen, in that order, the moment the page loads.
 */

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import AccessModeCard from "../components/admin/AccessModeCard";
import ModelCard from "../components/admin/ModelCard";
import UsageCard from "../components/admin/UsageCard";
import UsersCard from "../components/admin/UsersCard";
import type { AccessMode, AdminModels, AdminOverview, AdminSettings } from "../lib/admin";
import { errorText, fetchModels, fetchOverview, saveSettings } from "../lib/admin";
import { ApiError } from "../lib/api";
import type { AppConfig } from "../lib/config";
import AdminSignIn from "../components/admin/AdminSignIn";

export default function AdminPage({ config }: { config: AppConfig }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  // The price list AND the price-list version, together: `GET /api/admin/models` stamps the
  // version onto every option, and the usage card labels its totals with it.
  const [models, setModels] = useState<AdminModels>({ models: [], pricing_version: null });
  // "The server will not talk to you" — a stranger, an ordinary user or a guest.
  const [needsSignIn, setNeedsSignIn] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);

  const [modeBusy, setModeBusy] = useState(false);
  const [modeError, setModeError] = useState<string | null>(null);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const data = await fetchOverview();
      setOverview(data);
      setRefreshedAt(new Date());
    } catch (cause: unknown) {
      // 404 is the "this route does not exist for you" answer, not a failure to report.
      if (cause instanceof ApiError && cause.status === 404) {
        setNeedsSignIn(true);
        return;
      }
      setLoadError(errorText(cause));
      return;
    }
    try {
      setModels(await fetchModels());
    } catch {
      // The price table is context, not content: without it the select still lists the
      // model that is running and the page stays usable.
      setModels({ models: [], pricing_version: null });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const applySettings = async (
    patch: Partial<AdminSettings>,
    setBusy: (value: boolean) => void,
    setError: (value: string | null) => void,
  ) => {
    setBusy(true);
    setError(null);
    try {
      // `saveSettings` returns only the keys the server CONFIRMED, each read back out of the
      // database after the write — so this merge is "what is now in effect", not "what I
      // asked for". Anything it did not confirm keeps the value the last load reported.
      const confirmed = await saveSettings(patch);
      setOverview((current) =>
        current ? { ...current, settings: { ...current.settings, ...confirmed } } : current,
      );
    } catch (cause: unknown) {
      setError(errorText(cause));
    } finally {
      setBusy(false);
    }
  };

  if (needsSignIn) {
    // A fresh superuser session changes what /api/config says about `user`, which is what
    // reveals the ✦ in the header — so reload the whole app rather than just refetching here.
    return <AdminSignIn onSignedIn={() => window.location.reload()} />;
  }

  if (loadError !== null) {
    return (
      <div className="flex h-full items-center justify-center px-4">
        <div className="card max-w-sm p-6 text-center">
          <p className="text-sm font-medium">Не вдалося завантажити панель</p>
          <p className="mt-2 font-mono text-xs text-slate-500 dark:text-slate-400">{loadError}</p>
          <button type="button" className="btn-primary mt-4" onClick={() => void load()}>
            Спробувати ще раз
          </button>
        </div>
      </div>
    );
  }

  if (overview === null) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-slate-500 dark:text-slate-400">Завантаження…</p>
      </div>
    );
  }

  return (
    <div className="mx-auto h-full w-full max-w-5xl overflow-y-auto px-4 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold tracking-tight">Адміністрування</h1>
        <span className="text-sm text-slate-500 dark:text-slate-400">{config.user?.username}</span>
        <div className="ml-auto flex items-center gap-3">
          {refreshedAt && (
            <span className="hidden text-xs text-slate-400 dark:text-slate-500 sm:inline tabular-nums">
              оновлено {refreshedAt.toLocaleTimeString("uk-UA")}
            </span>
          )}
          <button type="button" className="btn-ghost" onClick={() => void load()}>
            Оновити
          </button>
          <Link to="/" className="btn-quiet">
            До класифікатора
          </Link>
        </div>
      </div>

      <div className="mt-4 space-y-4 pb-8">
        <AccessModeCard
          mode={overview.settings.access_mode}
          busy={modeBusy}
          error={modeError}
          onChange={(mode: AccessMode) =>
            void applySettings({ access_mode: mode }, setModeBusy, setModeError)
          }
        />

        <UsageCard usage={overview.usage} pricingVersion={models.pricing_version} />

        <ModelCard
          settings={overview.settings}
          models={models.models}
          build={overview.build}
          busy={modelBusy}
          error={modelError}
          onSave={(patch) => void applySettings(patch, setModelBusy, setModelError)}
        />

        <UsersCard currentUsername={config.user?.username ?? ""} />
      </div>
    </div>
  );
}
