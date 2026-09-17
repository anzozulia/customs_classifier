/**
 * The chat itself.
 *
 * Everything visible inside this component's box is rendered by ChatKit in a CROSS-ORIGIN
 * IFRAME served from cdn.platform.openai.com. Tailwind — ours or anyone's — cannot style a
 * single pixel of it: stylesheets do not cross document boundaries, and this is stronger
 * than a shadow root because `contentDocument` is not reachable either. The `theme` option
 * below is the entire styling surface for the transcript, composer, header and history
 * panel. Tailwind's job stops at the border of the box.
 */

import { ChatKit, useChatKit } from "@openai/chatkit-react";
import type { UseChatKitReturn } from "@openai/chatkit-react";
import { useCallback, useEffect, useRef, useState } from "react";

// `import type`, not `import`: @openai/chatkit ships NO runtime. Its package `exports` map
// has only a "types" condition, so a value import type-checks and then fails at bundle time
// with ERR_PACKAGE_PATH_NOT_EXPORTED. `verbatimModuleSyntax` in tsconfig keeps this honest.
import type { StartScreenPrompt } from "@openai/chatkit";

import { redirectToLogin } from "../lib/api";
import type { AppConfig } from "../lib/config";
import type { ColorScheme } from "../lib/theme";

export type ChatStatus = "loading" | "ready" | "streaming" | "error";

const THREAD_STORAGE_KEY = "uktzed:thread-id";

/** Must match NEW_CLASSIFICATION_ACTION in app/agent/tools_terminal.py. */
const NEW_CLASSIFICATION_ACTION = "new_classification";

const PROMPTS: StartScreenPrompt[] = [
  // `icon` is a ChatKitIcon — a closed union of built-in names (plus `lucide:*`). Several
  // names that appear in the public docs are not in it; these two are, checked against the
  // shipped index.d.ts.
  {
    label: "Кавоварка",
    prompt: "Побутова електрична кавоварка з помпою, 1350 Вт, для дому",
    icon: "search",
  },
  {
    label: "Футболка",
    prompt: "Чоловіча футболка, 100% бавовна, трикотаж, короткий рукав",
    icon: "search",
  },
  {
    label: "ПВХ плівка",
    prompt: "Самоклейна плівка з полівінілхлориду, 15 см завширшки, у рулоні",
    icon: "cube",
  },
];

function readStoredThread(): string | null {
  try {
    return window.localStorage.getItem(THREAD_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Remove `?thread=` from the address bar in place — no navigation, no remount. */
function dropThreadParam(): void {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("thread")) return;
    url.searchParams.delete("thread");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  } catch {
    // A locked-down history API is not worth failing the click over.
  }
}

function storeThread(threadId: string | null): void {
  try {
    if (threadId) window.localStorage.setItem(THREAD_STORAGE_KEY, threadId);
    else window.localStorage.removeItem(THREAD_STORAGE_KEY);
  } catch {
    // Reopening the last thread is a convenience, not a requirement.
  }
}

type Props = {
  config: AppConfig;
  scheme: ColorScheme;
  /**
   * From `/?thread=…` — how a /history row reopens its conversation. Read once, at mount:
   * every link that sets it comes from another route, so arriving here always remounts.
   */
  threadId?: string | null;
  /** Optional: the shell renders no status chip, but a host that wants one can pass this. */
  onStatusChange?: (status: ChatStatus) => void;
};

export default function ChatKitPanel({
  config,
  scheme,
  threadId,
  onStatusChange,
}: Props) {
  const notifyStatus = onStatusChange ?? (() => {});
  // Frozen at mount on purpose. `initialThread` is read once by the frame; feeding it a
  // changing value would be noise. A ?thread= in the URL wins over the last thread used.
  const [initialThread] = useState<string | null>(() => threadId ?? readStoredThread());

  /**
   * The ONLY place an expired session is observable. ChatKit denies `HttpError` and
   * `NetworkError` to the `chatkit` profile, so a 401 on /chatkit never reaches `onError` —
   * the user would just watch their message vanish.
   */
  const chatFetch = useCallback<typeof fetch>(async (input, init) => {
    const response = await fetch(input, { ...init, credentials: "same-origin" });
    // 401 only — see the note in lib/api.ts. A 403 is an Origin mismatch, and bouncing a
    // public-mode visitor to a login page they do not need is a dead end; letting ChatKit
    // surface the failed request at least says something happened.
    if (response.status === 401) redirectToLogin();
    return response;
  }, []);

  // The action handler lives inside the options object that produces `control`, so it cannot
  // close over `control` directly. A ref breaks the cycle and always sees the current one.
  const chatkitRef = useRef<UseChatKitReturn | null>(null);

  const chatkit = useChatKit({
    api: {
      // One URL for every operation — the custom client never appends a path; the operation
      // is `body.type`. Same origin, so the session cookie rides along and there is no CORS.
      url: config.chatkitUrl,
      // Required even in dev. On localhost / 127.0.0.1 / a non-standard port the check is
      // skipped with a console warning; in production a bad key makes the frame REMOVE
      // ITSELF from the DOM, with no degraded mode.
      domainKey: config.domainKey,
      fetch: chatFetch,
      // NOT SET: getClientSecret. Its mere presence would switch ChatKit to the
      // OpenAI-hosted client. Its absence is what selects the self-hosted one.
    },
    locale: config.locale,
    frameTitle: "Класифікатор УКТЗЕД",
    initialThread,
    theme: {
      colorScheme: scheme,
      radius: "round",
      density: "normal",
      typography: { baseSize: 15 },
      color: {
        grayscale: { hue: 220, tint: 6, shade: scheme === "dark" ? -1 : -4 },
        accent: { primary: scheme === "dark" ? "#f8fafc" : "#0f172a", level: 1 },
      },
      // NOT SET: theme.unsafeVariables. It exists (the frame stylesheet declares 983 custom
      // properties) and it is undocumented, unversioned and behind an auto-updating CDN
      // bundle. Skipping it is a decision, not an oversight.
    },
    header: { enabled: true, title: { enabled: true, text: "Класифікація товару" } },
    // Off deliberately. The chat window is for ONE product; past work lives on /history,
    // which shows codes and paths rather than conversation titles and can be searched and
    // exported. Disabling it also drops a threads.list {limit: 9999} the frame otherwise
    // issued on EVERY mount, before the user had done anything.
    history: { enabled: false },
    thread: { autoScroll: true },
    widgets: {
      // The server appends a "Класифікувати наступний товар" button under every finished
      // classification. handler="client" means it never reaches the backend: a new product
      // is a new thread, so we just switch to one. `setThreadId(null)` is ChatKit's own
      // documented way to start a fresh thread.
      onAction: async (action) => {
        if (action.type !== NEW_CLASSIFICATION_ACTION) return;
        await chatkitRef.current?.setThreadId(null);
        // `?thread=` is read once at mount to restore a conversation opened from /history.
        // Leaving it in the URL after switching away means a refresh silently reopens the
        // OLD thread — so drop it, without a navigation that would remount the frame.
        dropThreadParam();
      },
    },
    startScreen: {
      greeting: "Опишіть товар — підберу код УКТЗЕД.",
      prompts: PROMPTS,
    },
    composer: {
      placeholder: "Опишіть товар: матеріал, призначення, спосіб виготовлення…",
      // `attachments` omitted entirely => attachments disabled (the default).
    },
    // Feedback stays off: items.feedback bypasses the Store, so it is outside the server's
    // ownership boundary until add_feedback does its own check.
    threadItemActions: { feedback: false, retry: true },
    disclaimer: { text: "Довідкова класифікація. Остаточне рішення ухвалює митниця." },

    onReady: () => notifyStatus("ready"),
    onResponseStart: () => notifyStatus("streaming"),
    onResponseEnd: () => notifyStatus("ready"),
    onThreadChange: ({ threadId: current }) => storeThread(current),
    onError: ({ error }) => {
      // Reaches us only for the ten allowed error names (StreamError, FatalAppError, …).
      // HTTP and network failures are invisible here by design — see chatFetch above.
      console.error("[chatkit]", error.name, error.message);
      notifyStatus("error");
    },
  });

  useEffect(() => {
    // `setThreadId` lives on the hook's return value (ChatKitMethods), not on `control`.
    chatkitRef.current = chatkit;
  }, [chatkit]);

  // `:host` is height:100%/width:100%, so an unsized parent collapses the chat to zero
  // height. The definite height comes from the page layout.
  return <ChatKit control={chatkit.control} className="block h-full w-full" />;
}
