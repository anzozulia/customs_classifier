/**
 * The app's one not-found state.
 *
 * It is deliberately shared: `/admin` renders THIS when the server answers 404, so a
 * non-superuser who guesses the URL sees exactly what they would see for `/nonsense`.
 * A dedicated «ви не адміністратор» screen would confirm that the route exists, which is
 * the one thing a hidden panel must not do.
 */

import { Link } from "react-router-dom";

export default function NotFoundPage() {
  return (
    <div className="flex h-full items-center justify-center px-4">
      <div className="card max-w-sm p-6 text-center">
        <p className="text-sm font-medium">Сторінку не знайдено</p>
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
          Такої адреси в застосунку немає.
        </p>
        <Link to="/" className="btn-primary mt-4">
          До класифікатора
        </Link>
      </div>
    </div>
  );
}
