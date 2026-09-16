/**
 * The hidden admin panel. Reached by typing /admin; linked from nowhere except the ✦ in the
 * header, which only a superuser is shown.
 *
 * Hiding is done by the SERVER answering 404 to `GET /api/admin/overview`, and this page
 * renders the app's ordinary not-found state when it does — the same component `/nonsense`
 * renders, inside the same shell. There is deliberately no «у вас немає доступу» branch:
 * that message is itself the disclosure.
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
import NotFoundPage from "./NotFoundPage";

export default function AdminPage({ config }: { config: AppConfig }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  // The price list AND the price-list version, together: `GET /api/admin/models` stamps the
  // version onto every option, and the usage card labels its totals with it.
  const [models, setModels] = useState<AdminModels>({ models: [], pricing_version: null });
  const [notFound, setNotFound] = useState(false);
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
        setNotFound(true);
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

  if (notFound) {
    return <NotFoundPage />;
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
