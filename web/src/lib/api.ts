/**
 * The one place the shell talks to the Python app, and the one place a dead session is
 * turned into a redirect.
 *
 * Why an interceptor at all: ChatKit's own error channel is blind to HTTP failures —
 * `HttpError` and `NetworkError` are on the denylist for the `chatkit` profile, so a 401
 * on `/chatkit` never reaches `onError`. The only observation points are this wrapper and
 * the custom `api.fetch` we hand to ChatKit (see ChatKitPanel.tsx). Both call
 * `redirectToLogin`.
 */

export const LOGIN_PATH = "/login";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Hard navigation, not a router push: it also tears down the ChatKit iframe and its state. */
export function redirectToLogin(): void {
  if (window.location.pathname === LOGIN_PATH) return;
  const next = window.location.pathname + window.location.search;
  window.location.assign(`${LOGIN_PATH}?next=${encodeURIComponent(next)}`);
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");

  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });

  // 401 ONLY. A 403 here comes from `require_same_origin` — the Origin header did not match
  // PUBLIC_BASE_URL — which is a configuration or deployment problem, not an expired session.
  // Sending it to /login produced the worst possible loop: in public mode that page correctly
  // says «входити не потрібно», so the user was bounced to a screen telling them there was
  // nothing to do there, with no way forward. Surface it instead.
  if (response.status === 401) {
    redirectToLogin();
    throw new ApiError(response.status, "Сесія завершилася. Увійдіть знову.");
  }
  if (response.status === 403) {
    throw new ApiError(
      response.status,
      "Запит відхилено (перевірка джерела). Відкрийте застосунок за адресою, налаштованою " +
        "в PUBLIC_BASE_URL.",
    );
  }
  return response;
}

export async function apiJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response.ok) {
    throw new ApiError(response.status, await readErrorMessage(response));
  }
  return (await response.json()) as T;
}

/** FastAPI puts the text in `detail`; our own handlers use `message`. Accept either. */
async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body !== null && typeof body === "object") {
      const record = body as Record<string, unknown>;
      const text = record.detail ?? record.message;
      if (typeof text === "string" && text.length > 0) return text;
    }
  } catch {
    // Not JSON (an HTML error page from the proxy, say). Fall through to the status text.
  }
  return `Помилка ${response.status}`;
}
