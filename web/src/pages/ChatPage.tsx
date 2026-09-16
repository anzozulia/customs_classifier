import { useSearchParams } from "react-router-dom";

import ChatKitPanel from "../components/ChatKitPanel";
import type { AppConfig } from "../lib/config";
import type { ColorScheme } from "../lib/theme";

export default function ChatPage({ config, scheme }: { config: AppConfig; scheme: ColorScheme }) {
  const [params] = useSearchParams();

  return (
    <div className="mx-auto flex h-full w-full max-w-5xl flex-col px-4 py-4">
      {/* Definite height for the chat comes from here: min-h-0 + flex-1 inside a full-height
          column. `<openai-chatkit>` is height:100%, so an auto-height parent shows nothing. */}
      <div className="card min-h-0 flex-1 overflow-hidden">
        <ChatKitPanel config={config} scheme={scheme} threadId={params.get("thread")} />
      </div>
    </div>
  );
}
