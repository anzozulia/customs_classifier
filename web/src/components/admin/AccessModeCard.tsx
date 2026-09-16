/**
 * The kill switch.
 *
 * There are no spend caps and no rate limits in this app by decision, so this toggle is the
 * only lever the author has during a live demo. Two things follow, and they drive every
 * choice here:
 *
 *   1. The CURRENT state has to be readable from across a room — hence the colour band, the
 *      dot and a 2xl word, not a checkbox with a label.
 *   2. Closing access is the safe direction, so it is ONE click with no confirmation.
 *      Opening it is the dangerous one, so it takes a second, explicit click that spells out
 *      what changes. A symmetric confirm would put a dialog between the author and the
 *      emergency stop, which is exactly backwards.
 */

import { useState } from "react";

import type { AccessMode } from "../../lib/admin";

type Props = {
  mode: AccessMode;
  busy: boolean;
  error: string | null;
  onChange: (mode: AccessMode) => void;
};

export default function AccessModeCard({ mode, busy, error, onChange }: Props) {
  const [arming, setArming] = useState(false);
  const isPublic = mode === "public";

  return (
    <section className="card overflow-hidden">
      <div className={isPublic ? "h-1.5 bg-amber-400" : "h-1.5 bg-emerald-500"} />

      <div className="p-5">
        <div className="flex flex-wrap items-center gap-4">
          <span
            aria-hidden="true"
            className={[
              "h-3 w-3 flex-none rounded-full",
              isPublic ? "bg-amber-400 ring-4 ring-amber-400/25" : "bg-emerald-500 ring-4 ring-emerald-500/20",
            ].join(" ")}
          />
          <div>
            <h2 className="text-xs font-medium uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Режим доступу
            </h2>
            <p className="text-2xl font-semibold tracking-tight">
              {isPublic ? "Публічний" : "За паролем"}
            </p>
          </div>

          <div className="ml-auto">
            {isPublic ? (
              <button
                type="button"
                className="btn-primary"
                disabled={busy}
                onClick={() => {
                  setArming(false);
                  onChange("private");
                }}
              >
                {busy ? "Застосовуємо…" : "Закрити доступ"}
              </button>
            ) : (
              <button
                type="button"
                className="btn-ghost"
                disabled={busy || arming}
                onClick={() => setArming(true)}
              >
                Відкрити публічний доступ
              </button>
            )}
          </div>
        </div>

        {arming && !isPublic && (
          <div className="mt-4 rounded-lg border border-amber-300 bg-amber-50 p-4 dark:border-amber-500/40 dark:bg-amber-500/10">
            <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
              Відкрити застосунок для всіх?
            </p>
            <ul className="mt-2 space-y-1 text-sm text-amber-900/90 dark:text-amber-200/90">
              <li>• Будь-хто з посиланням користуватиметься класифікатором без входу.</li>
              <li>• Кожен відвідувач отримає власну гостьову сесію та власну історію.</li>
              <li>• Обмежень на витрати немає — стежте за розділом «Використання».</li>
            </ul>
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                className="btn border-amber-600 bg-amber-600 text-white hover:bg-amber-500"
                disabled={busy}
                onClick={() => {
                  setArming(false);
                  onChange("public");
                }}
              >
                {busy ? "Застосовуємо…" : "Так, відкрити доступ"}
              </button>
              <button type="button" className="btn-ghost" onClick={() => setArming(false)}>
                Скасувати
              </button>
            </div>
          </div>
        )}

        {error && (
          <p role="alert" className="mt-4 text-sm text-rose-600 dark:text-rose-400">
            {error}
          </p>
        )}

        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          <ModeExplainer
            active={isPublic}
            title="Публічний"
            accent="amber"
            lines={[
              "Вхід не потрібен.",
              "Кожен відвідувач — окрема гостьова сесія з власною історією.",
              "Історія зберігається у браузері відвідувача між візитами.",
            ]}
          />
          <ModeExplainer
            active={!isPublic}
            title="За паролем"
            accent="emerald"
            lines={[
              "Потрібен логін і пароль.",
              "Гостьові сесії перестають працювати негайно.",
              "Дані гостей залишаються в базі, але недоступні без входу.",
            ]}
          />
        </div>
      </div>
    </section>
  );
}

function ModeExplainer({
  active,
  title,
  accent,
  lines,
}: {
  active: boolean;
  title: string;
  accent: "amber" | "emerald";
  lines: string[];
}) {
  const ring =
    accent === "amber"
      ? "border-amber-300 bg-amber-50/60 dark:border-amber-500/40 dark:bg-amber-500/5"
      : "border-emerald-300 bg-emerald-50/60 dark:border-emerald-500/40 dark:bg-emerald-500/5";

  return (
    <div
      className={[
        "rounded-lg border p-3",
        active ? ring : "border-slate-200 bg-slate-50/60 dark:border-slate-800 dark:bg-slate-950/40",
      ].join(" ")}
    >
      <p className="flex items-center gap-2 text-sm font-medium">
        {title}
        {active && (
          <span className="text-xs font-normal text-slate-500 dark:text-slate-400">
            — зараз увімкнено
          </span>
        )}
      </p>
      <ul
        className={[
          "mt-2 space-y-1 text-xs",
          active ? "text-slate-700 dark:text-slate-300" : "text-slate-500 dark:text-slate-400",
        ].join(" ")}
      >
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}
