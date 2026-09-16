# web/ — the shell around ChatKit

React 19 + Vite 7 + Tailwind 3, matching the four apps in
[`openai-chatkit-advanced-samples`](https://github.com/openai/openai-chatkit-advanced-samples).
Three pages and a layout: `/login`, `/` (the chat), `/history` (+ `/history/:id`).

It is a **shell**. The chat itself is drawn by ChatKit inside a cross-origin iframe; this app
supplies the page around it, the domain key, the session cookie and the audit view.

```
npm install
npm run dev        # http://127.0.0.1:5173, proxying /api and /chatkit to :8000
npm run build      # tsc --noEmit && vite build  ->  web/dist
npm run typecheck
```

`BACKEND_URL` overrides the proxy target (default `http://127.0.0.1:8000`).
`ALLOWED_HOSTS` is a comma-separated list added to Vite's `server.allowedHosts`, needed when
you expose the dev server through ngrok/cloudflared.

---

## What the server must provide

The frontend touches exactly seven endpoints. Everything is same-origin; the session cookie
is the only credential.

### `GET /api/config` — public, always 200

Read once on mount, before anything renders.

```json
{
  "domain_key": "domain_pk_…",
  "locale": "uk-UA",
  "chatkit_url": "/chatkit",
  "user": { "username": "anton" }
}
```

`user` is `null` when there is no session — that is how `/login` renders without a 401 loop,
and it is why this endpoint must not require auth. The domain key is a **public** value; it
ships in the page either way. camelCase keys (`domainKey`, `chatkitUrl`) are accepted too.
A missing `domain_key` is a hard error with a visible message, because a ChatKit frame
without one deletes itself from the DOM and leaves an empty box.

### `POST /api/login` — form-encoded

Body is `application/x-www-form-urlencoded` with `username` and `password`, matching the
`Form(...)` handler in the architecture blueprint (and the pinned `python-multipart`).
Success is *any* 2xx: a `204`, or a `303` to `/` that `fetch` follows transparently. `401`
renders "невірне ім'я користувача або пароль"; anything else shows the status code.
After success the app does a **hard navigation**, not a router push, so `/api/config` is
re-fetched under the new session.

### `POST /api/logout`
Any 2xx. The app navigates to `/login` regardless of the outcome.

### `GET /api/history?q=&before=&limit=`

```json
{
  "items": [
    {
      "id": "…", "created_at": "2026-09-16T10:42:00Z",
      "input_text": "самоклейна ПВХ плівка 15 см у рулоні",
      "outcome": "classified",            // classified | clarification | error | pending
      "duration_ms": 11300, "thread_id": "thr_…",
      "codes": [{ "code": "3919101200", "description": "…", "full_path": "…", "is_primary": true }],
      "clarification_question": null, "error_class": null
    }
  ],
  "next_before": "…"                       // null on the last page
}
```

Keyset pagination — `next_before` is fed back as `before`. Every field past `id` is optional
in the TypeScript types, because a `classification` row is INSERTed pending before the model
runs and can legitimately have no codes yet.

### `GET /api/history/{id}`
The same object plus `model`, `prompt_version`, `dataset_sha256`, `tokens_in`, `tokens_out`,
`cost_usd`, and `tool_calls: [{name, arguments, summary, duration_ms, ok, error}]`.

### `GET /api/history/export.csv?q=`
Linked from the history toolbar as a plain download.

### `POST /chatkit`
ChatKit's single endpoint. Not called by this code directly — ChatKit calls it, through the
custom `fetch` in `src/components/ChatKitPanel.tsx`.

**401 / 403 on any of these redirects to `/login?next=…`**, from one of two places:
`src/lib/api.ts` for our own calls and the custom `fetch` for ChatKit's. There is no third.

---

## Known constraints

### The ChatKit runtime is not bundled, and cannot be self-hosted

`index.html` loads it from a fixed URL:

```html
<script src="https://cdn.platform.openai.com/deployments/chatkit/chatkit.js" async></script>
```

This is not a preference.

* **The npm package has no runtime.** `@openai/chatkit@1.9.0` ships six files, three of them
  `.d.ts`. Its `exports` map has a `"types"` condition and nothing else, so
  `import '@openai/chatkit'` **type-checks and then fails at bundle time** with
  `ERR_PACKAGE_PATH_NOT_EXPORTED`. Every import from it in this app is `import type`, and
  `verbatimModuleSyntax` in `tsconfig.json` makes that a compile error to get wrong.
* **Re-hosting the script breaks it.** The loader derives the URL of the iframe it mounts from
  `document.currentScript.src`. Serve it from your own domain and it looks for the frame
  document under your domain, where it does not exist. Inlining it, renaming it, or importing
  it as a module fails the same way. OpenAI state self-hosting the script is unsupported.
* **You cannot pin it.** The URL is unversioned (`cache-control: max-age=300`) and
  auto-updates within minutes of a publish. npm pins the *types you compile against*, not the
  runtime beneath them. Mitigation is the whole of: pin the types exactly, watch the
  changelog, log `chatkit.error`. There is no rollback. (Softening fact: 1.0.0 → 1.9.0 is
  nine releases with zero breaking entries.)
* **The app is not self-contained.** Every load pulls the 28 KB loader, a ~4 MB frame bundle
  and a ~288 KB stylesheet from `cdn.platform.openai.com`, and in production one call to
  `api.openai.com` to verify the domain key. **Offline/air-gapped operation is impossible**, a
  CDN outage takes the chat down, and a failed domain check *removes the element from the DOM*
  with no degraded mode. This is the honest cost of "self-hosted agent, hosted UI".

### The domain key is fetched at runtime, not inlined at build time

The official samples read it from `import.meta.env.VITE_CHATKIT_API_DOMAIN_KEY`. Vite inlines
`VITE_*` into the JS bundle at build time, which makes "serve this from a different hostname"
a rebuild-and-redeploy. This app fetches `GET /api/config` on mount instead and renders a
loading state until it resolves — the key is public either way, so build-time inlining buys
nothing but a build step. `import.meta.env` is not read anywhere in `src/`.

Register the production hostname at
`platform.openai.com/settings/organization/security/domain-allowlist` **days** before the
deploy: propagation has been reported at anywhere from a few minutes to ~30. One key covers
20 domains, so prod and staging share one. On `localhost`, `127.0.0.1`, `*.local` or any
non-standard port the check is skipped with a console warning, which is why
`domain_pk_localhost_dev` works in dev.

### Tailwind cannot style the chat. Not one rule.

The transcript, composer, header, history panel and toasts live in an iframe served from
`cdn.platform.openai.com` — a different origin. Stylesheets do not cross document boundaries,
and this is stronger than a shadow root: `contentDocument` is not reachable either.

| Surface | Styled by |
|---|---|
| `/login`, the app header, `/history`, `/history/:id`, the box that sizes the chat | **Tailwind** |
| Transcript, bubbles, composer, thread history panel, error toasts | **ChatKit `theme` only** |

Theming goes through `theme` in `ChatKitPanel.tsx`:
`{ colorScheme, radius, density, typography: { baseSize }, color: { grayscale, accent, surface } }`.
Web fonts load *inside* the frame via `theme.typography.fontSources`; an `@font-face` in our
CSS does not reach it. There is an undocumented `theme.unsafeVariables` escape hatch (the
frame stylesheet declares 983 custom properties); it is deliberately unused — private,
unversioned internals behind an auto-updating bundle.

**Light/dark is two mechanisms that must not drift**, so `src/lib/theme.ts` owns both: a
`dark` class on `<html>` for Tailwind (`darkMode: "class"`), and `theme.colorScheme` for
ChatKit. ChatKit does **not** follow `prefers-color-scheme` on its own — its default is
`"light"` forever — so the media query is watched here and pushed into both. An explicit
toggle is remembered in `localStorage` and wins over the OS.

One more sizing trap: `<openai-chatkit>` is `:host { height: 100%; width: 100% }`, so an
auto-height parent collapses it to nothing. `ChatPage` gives it a definite height with
`min-h-0 flex-1` inside a full-height column.

### Ukrainian is a real ChatKit locale

`locale: 'uk-UA'` is a translation file, not an English fallback, so the composer
placeholder, retry/feedback tooltips, history panel and error toasts are Ukrainian for free.
The frame also sends `Accept-Language: uk-UA` to `/chatkit` on every call.

---

## Deploying

`npm run build` emits `web/dist`. Serve it statically from the Python app or Caddy, on the
**same origin** as `/api` and `/chatkit` — that is what makes the relative `/chatkit` work,
keeps CORS out of the picture entirely, and lets the session cookie ride along.

The router is `BrowserRouter`, so **unknown paths must fall back to `index.html`** or a reload
on `/history/<id>` 404s. The dev server already does this.

CSP needs `script-src https://cdn.platform.openai.com`, `frame-src
https://cdn.platform.openai.com` and `connect-src https://api.openai.com` — the frame is
loaded by the browser, and the domain check is an outbound call from it.

## Deliberately not here

No state-management library, no component library, no design system, no icon package (two
inline SVGs), no ESLint config, no test runner. Config is one `fetch` and props; the four
values that cross component boundaries (`config`, `scheme`, `toggle`, `status`) travel one
level, so a context would be indirection without a payer. Tailwind is v3 with
`tailwind.config.js` + `postcss.config.js` because that is what the official samples are
written against; v4's CSS-first config would mean their snippets stop being copy-paste.
