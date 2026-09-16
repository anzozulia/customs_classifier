import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import ChatKitPanel, { type ChatStatus } from "../components/ChatKitPanel";
import type { AppConfig } from "../lib/config";
import type { ColorScheme } from "../lib/theme";

const STATUS_LABEL: Record<ChatStatus, string> = {
  loading: "завантаження…",
  ready: "готово",
  streaming: "думаю…",
  error: "помилка",
};

export default function ChatPage({ config, scheme }: { config: AppConfig; scheme: ColorScheme }) {
  const [status, setStatus] = useState<ChatStatus>("loading");
  const [params] = useSearchParams();

  return (
    <div className="mx-auto flex h-full w-full max-w-5xl flex-col gap-3 px-4 py-4">
      <div className="flex flex-none items-center justify-between text-xs text-slate-500 dark:text-slate-400">
        <span>
          Статус:{" "}
          <span className={status === "error" ? "text-rose-600 dark:text-rose-400" : undefined}>
            {STATUS_LABEL[status]}
          </span>
        </span>
        <span className="font-mono">{config.locale}</span>
      </div>

      {/* Definite height for the chat comes from here: min-h-0 + flex-1 inside a full-height
          column. `<openai-chatkit>` is height:100%, so an auto-height parent shows nothing. */}
      <div className="card min-h-0 flex-1 overflow-hidden">
        <ChatKitPanel
          config={config}
          scheme={scheme}
          threadId={params.get("thread")}
          onStatusChange={setStatus}
        />
      </div>
    </div>
  );
}
