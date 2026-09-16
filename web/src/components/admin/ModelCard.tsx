/**
 * Model + reasoning effort, and the read-only facts about what is running right now.
 *
 * The price table under the select is not decoration: the two models differ by 10x, so
 * "which one is selected" and "what that costs per 1M tokens" are the same question. The
 * prompt version, prompt sha and dataset sha are shown because they are what makes an old
 * classification row reproducible — they are facts, not settings, so there is no control
 * next to them.
 */

import { useEffect, useState } from "react";

import type { AdminBuild, AdminModel, AdminSettings } from "../../lib/admin";
import { REASONING_EFFORTS, formatPerMtok, shortSha } from "../../lib/admin";

type Props = {
  settings: AdminSettings;
  models: AdminModel[];
  build: AdminBuild;
  busy: boolean;
  error: string | null;
  onSave: (patch: Partial<AdminSettings>) => void;
};

export default function ModelCard({ settings, models, build, busy, error, onSave }: Props) {
  const [model, setModel] = useState(settings.model);
  const [effort, setEffort] = useState(settings.reasoning_effort);

  // The saved values are the source of truth; re-sync whenever they change under us (a save
  // that the server adjusted, or a reload).
  useEffect(() => {
    setModel(settings.model);
    setEffort(settings.reasoning_effort);
  }, [settings.model, settings.reasoning_effort]);

  const dirty = model !== settings.model || effort !== settings.reasoning_effort;

  // A model set through the environment need not be in the price list; never silently drop
  // the value that is actually running out of its own select.
  const options: AdminModel[] = models.some((item) => item.id === settings.model)
    ? models
    : [
        {
          id: settings.model,
          selected: true,
          input_per_mtok: null,
          cached_input_per_mtok: null,
          output_per_mtok: null,
        },
        ...models,
      ];

  const efforts = REASONING_EFFORTS.some((item) => item === settings.reasoning_effort)
    ? [...REASONING_EFFORTS]
    : [settings.reasoning_effort, ...REASONING_EFFORTS];

  return (
    <section className="card p-5">
      <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
        Модель
      </h2>

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="admin-model" className="mb-1 block text-sm font-medium">
            Модель
          </label>
          <select
            id="admin-model"
            className="field"
            value={model}
            disabled={busy}
            onChange={(event) => setModel(event.target.value)}
          >
            {options.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id}
                {item.input_per_mtok !== null && item.output_per_mtok !== null
                  ? ` — ${formatPerMtok(item.input_per_mtok)} / ${formatPerMtok(item.output_per_mtok)} за 1M`
                  : ""}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="admin-effort" className="mb-1 block text-sm font-medium">
            Зусилля міркування
          </label>
          <select
            id="admin-effort"
            className="field"
            value={effort}
            disabled={busy}
            onChange={(event) => setEffort(event.target.value)}
          >
            {efforts.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <button
          type="button"
          className="btn-primary"
          disabled={busy || !dirty}
          onClick={() => onSave({ model, reasoning_effort: effort })}
        >
          {busy ? "Зберігаємо…" : "Зберегти"}
        </button>
        {dirty && !busy && (
          <button
            type="button"
            className="btn-ghost"
            onClick={() => {
              setModel(settings.model);
              setEffort(settings.reasoning_effort);
            }}
          >
            Скинути
          </button>
        )}
        {error && (
          <p role="alert" className="text-sm text-rose-600 dark:text-rose-400">
            {error}
          </p>
        )}
      </div>

      {models.length > 0 && (
        <div className="mt-5 overflow-x-auto">
          <table className="w-full min-w-[26rem] text-left text-sm">
            <thead className="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
              <tr>
                <th className="py-1.5 pr-3 font-medium">Модель</th>
                <th className="py-1.5 pr-3 text-right font-medium">Вхід / 1M</th>
                <th className="py-1.5 pr-3 text-right font-medium">Кеш / 1M</th>
                <th className="py-1.5 text-right font-medium">Вихід / 1M</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {models.map((item) => {
                const current = item.id === settings.model;
                return (
                  <tr
                    key={item.id}
                    className={current ? "font-medium" : "text-slate-500 dark:text-slate-400"}
                  >
                    <td className="py-1.5 pr-3 font-mono text-xs">
                      {item.id}
                      {current && (
                        <span className="ml-2 font-sans text-xs font-normal text-slate-500 dark:text-slate-400">
                          активна
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {formatPerMtok(item.input_per_mtok)}
                    </td>
                    <td className="py-1.5 pr-3 text-right tabular-nums">
                      {formatPerMtok(item.cached_input_per_mtok)}
                    </td>
                    <td className="py-1.5 text-right tabular-nums">
                      {formatPerMtok(item.output_per_mtok)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <dl className="mt-5 flex flex-wrap gap-x-6 gap-y-2 border-t border-slate-100 pt-4 text-sm dark:border-slate-800">
        <Fact label="Версія промпту" value={build.prompt_version ?? "—"} />
        <Fact label="Промпт sha" value={shortSha(build.prompt_sha256)} mono />
        <Fact label="Дані sha" value={shortSha(build.dataset_sha256)} mono />
      </dl>
    </section>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wider text-slate-500 dark:text-slate-400">
        {label}
      </dt>
      <dd className={mono ? "mt-0.5 font-mono text-xs" : "mt-0.5"}>{value}</dd>
    </div>
  );
}
