/**
 * «Про проєкт»: what this is, what it does, how to use it and what it is built on — shown once,
 * automatically, to a browser that has never dismissed it, and reachable afterwards from the
 * header (an ⓘ button inline, a row in the burger menu).
 *
 * A NATIVE `<dialog>` opened with `showModal()`, not a positioned div. The page under it holds
 * ChatKit's cross-origin iframe, and `showModal()` is what makes everything outside the dialog
 * inert — iframe included — puts the dialog in the top layer above any z-index the frame or
 * the header can raise, traps Tab inside, and closes on Escape. A div would have to re-create
 * each of those, and the iframe's focus is the part it could not.
 *
 * Every way out — the button, ×, Escape, a click on the backdrop — ends in the dialog's
 * `close` event, so "dismissed" is recorded in exactly one place.
 *
 * The content is static on purpose. Nothing here names the model: it is switchable at runtime
 * from the admin panel, and a welcome text that quotes it would go stale the first time it is.
 */

import { type MouseEvent, type ReactNode, useEffect, useRef } from "react";

/** Bump the suffix when the content changes enough that returning visitors should see it again. */
const SEEN_STORAGE_KEY = "uktzed:welcome-seen:v1";

/**
 * "Has this browser already dismissed it?" An unreadable storage answers YES: if the dismissal
 * cannot be remembered, an automatic popup would come back on every single page load, which is
 * worse than not volunteering it. The header button still opens it.
 */
export function welcomeAlreadySeen(): boolean {
  try {
    return window.localStorage.getItem(SEEN_STORAGE_KEY) !== null;
  } catch {
    return true;
  }
}

function markWelcomeSeen(): void {
  try {
    window.localStorage.setItem(SEEN_STORAGE_KEY, new Date().toISOString());
  } catch {
    // Private mode / storage disabled — see welcomeAlreadySeen.
  }
}

const FEATURES: { title: string; body: string }[] = [
  {
    title: "Код за описом товару",
    body: "Замість опису можна вставити посилання на товар в інтернет-магазині — агент візьме характеристики зі сторінки. Далі проходить довідник так, як це робить декларант: розділ → група → позиція → 10-значний код. Хід пошуку видно наживо.",
  },
  {
    title: "Обґрунтована відповідь",
    body: "Код, пояснення простими словами, рівень впевненості, місце коду в довіднику, відхилені альтернативи з причинами та офіційний текст позиції.",
  },
  {
    title: "Не вигадує кодів",
    body: "Код можна видати лише тоді, коли його гілку відкрито в довіднику в цьому ж запиті. Інакше відповідь відхиляється ще до показу.",
  },
  {
    title: "Уточнює, коли бракує даних",
    body: "Якщо від матеріалу чи призначення залежить код — поставить одне питання з варіантами відповіді в один клік.",
  },
  {
    title: "Шукає в інтернеті",
    body: "Для незнайомих торгових назв, моделей і посилань на товар — пошук в інтернеті.",
  },
  {
    title: "Історія",
    body: "Кожна класифікація зберігається: пошук за описом чи кодом, журнал звернень агента до довідника, експорт у CSV і повернення до розмови.",
  },
];

const STEPS: string[] = [
  "Опишіть товар: що це, з чого зроблено, для чого призначено, як виготовлено. Або вставте посилання на нього в інтернет-магазині чи оберіть приклад на стартовому екрані.",
  "Стежте за пошуком у довіднику. Якщо агент поставить уточнювальне запитання, оберіть варіант або відповідайте своїми словами.",
  "Отримайте код з обґрунтуванням. Для нового товару натисніть «Класифікувати наступний товар».",
  "Усе, що ви класифікували, — у розділі «Історія».",
];

const STACK: { group: string; items: string[] }[] = [
  { group: "Агент", items: ["OpenAI Agents SDK", "Responses API", "Web Search Tool"] },
  { group: "Бекенд", items: ["Python 3.13", "FastAPI", "PostgreSQL 17", "pg_trgm"] },
  { group: "Інтерфейс", items: ["OpenAI ChatKit", "React 19", "Vite", "Tailwind CSS"] },
  { group: "Інфраструктура", items: ["Docker Compose", "GitHub Actions CI/CD", "nginx"] },
];

type Props = {
  /** A guest is told what a demo session means for them; a signed-in account is not. */
  isGuest: boolean;
  /** Mounted only while open; this is called once the dialog has actually closed. */
  onClose: () => void;
};

export default function WelcomeDialog({ isGuest, onClose }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const primaryRef = useRef<HTMLButtonElement>(null);
  // Where the current click STARTED. A text selection dragged from the panel out onto the
  // backdrop also ends in a click whose target is the <dialog>; that must not close it.
  const pressedOnBackdrop = useRef(false);
  // The latest handler, without re-running the effect that opens the dialog.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    const onDialogClose = () => {
      markWelcomeSeen();
      onCloseRef.current();
    };
    dialog.addEventListener("close", onDialogClose);

    // Guarded: StrictMode runs this effect twice, and older engines throw InvalidStateError
    // on a second showModal() rather than ignoring it.
    if (!dialog.open) dialog.showModal();
    // NOT the autoFocus prop: React implements it as a focus() call at commit, which runs
    // before showModal() and is then overridden by the dialog's own focusing steps (they
    // would pick the × in the corner). The primary action is the one Enter should press.
    primaryRef.current?.focus();

    return () => dialog.removeEventListener("close", onDialogClose);
  }, []);

  // The backdrop belongs to the <dialog> element itself, and the panels fill its box edge to
  // edge — so an event whose target is the <dialog> landed on the backdrop.
  const onBackdropPointerDown = (event: MouseEvent<HTMLDialogElement>) => {
    pressedOnBackdrop.current = event.target === event.currentTarget;
  };
  const onBackdropClick = (event: MouseEvent<HTMLDialogElement>) => {
    if (pressedOnBackdrop.current && event.target === event.currentTarget) {
      event.currentTarget.close();
    }
    pressedOnBackdrop.current = false;
  };

  const close = () => dialogRef.current?.close();

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="welcome-title"
      onPointerDown={onBackdropPointerDown}
      onClick={onBackdropClick}
      className={[
        // Reset the UA dialog box (border, padding, max sizes) and size it ourselves.
        "m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-2xl overflow-hidden p-0",
        "rounded-2xl border border-slate-200 bg-white text-slate-900 shadow-2xl",
        "dark:border-slate-800 dark:bg-slate-900 dark:text-slate-100",
        "backdrop:bg-slate-950/50 backdrop:backdrop-blur-[2px]",
        // `open:` so the flex layout applies only while shown; a closed <dialog> must stay
        // display:none, which an unconditional `flex` would override.
        "open:flex open:flex-col",
      ].join(" ")}
    >
      <header className="flex flex-none items-start gap-3 border-b border-slate-200 px-5 py-4 sm:px-6 dark:border-slate-800">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="welcome-title" className="text-lg font-semibold tracking-tight">
              Класифікатор УКТЗЕД
            </h2>
            {isGuest && (
              <span className="rounded-full border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-500 dark:border-slate-700 dark:text-slate-400">
                демо
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            ШІ-агент, що підбирає 10-значний код УКТЗЕД за текстовим описом товару — і показує,
            чому саме цей.
          </p>
        </div>
        <button type="button" onClick={close} className="btn-icon -mr-2 -mt-1" aria-label="Закрити">
          <svg viewBox="0 0 24 24" className="h-5 w-5 flex-none" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <path d="m6 6 12 12M18 6 6 18" />
          </svg>
        </button>
      </header>

      {/* The only scrolling region: header and footer stay put, so the way out is always on
          screen however short the phone. */}
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto px-5 py-5 sm:px-6">
        <Section title="Для чого">
          <p className="text-sm leading-relaxed text-slate-600 dark:text-slate-300">
            УКТЗЕД — Українська класифікація товарів зовнішньоекономічної діяльності: понад
            10{"\u00a0"}000 кінцевих кодів, а помилка в коді може обернутися донарахуванням мита та
            штрафами. Класифікатор бере на себе найдовшу частину роботи брокера чи імпортера —
            пошук у довіднику — і показує аргументи, щоб їх можна було перевірити.
          </p>
          <p className="mt-2 text-sm leading-relaxed text-slate-600 dark:text-slate-300">
            Результат довідковий — остаточне рішення щодо класифікації ухвалює митниця.
          </p>
        </Section>

        <Section title="Що вміє">
          <ul className="grid gap-x-5 gap-y-3 sm:grid-cols-2">
            {FEATURES.map((feature) => (
              <li key={feature.title} className="text-sm">
                <p className="font-medium">{feature.title}</p>
                <p className="mt-0.5 leading-snug text-slate-500 dark:text-slate-400">{feature.body}</p>
              </li>
            ))}
          </ul>
        </Section>

        <Section title="Як користуватися">
          <ol className="space-y-2.5">
            {STEPS.map((step, index) => (
              <li key={step} className="flex gap-3 text-sm leading-snug text-slate-600 dark:text-slate-300">
                <span
                  aria-hidden="true"
                  className="flex h-5 w-5 flex-none items-center justify-center rounded-full bg-slate-900 text-[11px] font-semibold tabular-nums text-white dark:bg-slate-100 dark:text-slate-900"
                >
                  {index + 1}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ol>
        </Section>

        <Section title="Технології">
          <dl className="space-y-2">
            {STACK.map((row) => (
              <div key={row.group} className="flex flex-col gap-1.5 sm:flex-row sm:items-baseline sm:gap-3">
                <dt className="w-28 flex-none text-xs font-medium text-slate-500 dark:text-slate-400">
                  {row.group}
                </dt>
                <dd className="flex flex-wrap gap-1.5">
                  {row.items.map((item) => (
                    <span
                      key={item}
                      className="rounded-md bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-200"
                    >
                      {item}
                    </span>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </Section>

        <p className="rounded-lg bg-slate-50 px-3 py-2.5 text-xs leading-relaxed text-slate-500 dark:bg-slate-800/60 dark:text-slate-400">
          {isGuest
            ? "Публічна демо-версія. Ваша історія прив'язана до цього браузера й доступна лише вам. "
            : "Ваша історія прив'язана до облікового запису й доступна лише вам. "}
          Описи товарів обробляє OpenAI — не вводьте конфіденційних даних.
        </p>
      </div>

      <footer className="flex flex-none items-center justify-end gap-2 border-t border-slate-200 px-5 py-3 sm:px-6 dark:border-slate-800">
        {/* h-11 below `sm`: .btn alone is 38px, under the 44px floor for a thumb. */}
        <button
          ref={primaryRef}
          type="button"
          onClick={close}
          className="btn-primary h-11 w-full sm:h-auto sm:w-auto"
        >
          Зрозуміло, почати
        </button>
      </footer>
    </dialog>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="mb-2.5 text-xs font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
        {title}
      </h3>
      {children}
    </section>
  );
}
