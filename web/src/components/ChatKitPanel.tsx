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
import { useCallback, useState } from "react";

// `import type`, not `import`: @openai/chatkit ships NO runtime. Its package `exports` map
// has only a "types" condition, so a value import type-checks and then fails at bundle time
// with ERR_PACKAGE_PATH_NOT_EXPORTED. `verbatimModuleSyntax` in tsconfig keeps this honest.
import type { StartScreenPrompt } from "@openai/chatkit";

import { redirectToLogin } from "../lib/api";
import type { AppConfig } from "../lib/config";
import type { ColorScheme } from "../lib/theme";

export type ChatStatus = "loading" | "ready" | "streaming" | "error";

const THREAD_STORAGE_KEY = "uktzed:thread-id";

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
  onStatusChange: (status: ChatStatus) => void;
};

export default function ChatKitPanel({ config, scheme, threadId, onStatusChange }: Props) {
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
    if (response.status === 401 || response.status === 403) redirectToLogin();
    return response;
  }, []);

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
    // With history enabled the frame issues threads.list {limit: 9999} on EVERY mount,
    // before the user does anything. The server's Store scopes it to the session user.
    history: { enabled: true, showDelete: true, showRename: true },
    thread: { autoScroll: true },
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

    onReady: () => onStatusChange("ready"),
    onResponseStart: () => onStatusChange("streaming"),
    onResponseEnd: () => onStatusChange("ready"),
    onThreadChange: ({ threadId: current }) => storeThread(current),
    onError: ({ error }) => {
      // Reaches us only for the ten allowed error names (StreamError, FatalAppError, …).
      // HTTP and network failures are invisible here by design — see chatFetch above.
      console.error("[chatkit]", error.name, error.message);
      onStatusChange("error");
    },
  });

  // `:host` is height:100%/width:100%, so an unsized parent collapses the chat to zero
  // height. The definite height comes from the page layout.
  return <ChatKit control={chatkit.control} className="block h-full w-full" />;
}
