import Image from "next/image";
import { cn } from "@/lib/utils";
import type { ChatMessage } from "@/types";

interface MessageBubbleProps {
  message: ChatMessage;
  pending?: boolean;
}

export function MessageBubble({ message, pending = false }: MessageBubbleProps) {
  const time = new Date(message.created_at).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

  // System message — centered banner
  if (message.role === "system") {
    return (
      <div className="flex justify-center py-1">
        <div className="flex items-center gap-2 rounded border border-hr-2 bg-bg-2 px-3 py-1.5">
          {message.attachment && (
            <div className="ph h-8 w-8 rounded text-[8px]">IMG</div>
          )}
          <span className="font-mono text-[10.5px] uppercase tracking-[0.06em] text-fg-3">
            {message.content}
          </span>
        </div>
      </div>
    );
  }

  const isUser = message.role === "user";

  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[78%] rounded-card px-3 py-2.5 text-sm leading-relaxed",
          isUser
            ? "bg-blue-tint border border-blue-line text-fg-1"
            : "bg-bg-3 border border-hr-2 text-fg-1",
          pending && "opacity-60",
        )}
      >
        {/* Image attachment preview */}
        {message.attachment?.type === "image" && (
          message.attachment.previewUrl ? (
            <div className="mb-2 flex flex-col gap-1">
              {/* Client-side object-URL preview: unoptimized (the Next loader can't
                  fetch a blob) + CSS sizing to preserve the original layout. */}
              <Image
                src={message.attachment.previewUrl}
                alt={message.attachment.filename}
                width={0}
                height={0}
                sizes="100vw"
                unoptimized
                className="rounded max-h-[200px] max-w-full w-auto h-auto object-contain"
              />
              <span className="font-mono text-[10px] text-fg-4 truncate">
                {message.attachment.filename}
              </span>
            </div>
          ) : (
            <div className="ph mb-2 h-16 w-full rounded text-[10px]">
              {message.attachment.filename}
            </div>
          )
        )}

        <p className="whitespace-pre-wrap">{message.content}</p>

        <time
          className={cn(
            "mt-1 block font-mono text-[10px]",
            isUser ? "text-blue-fg/60" : "text-fg-4",
          )}
        >
          {time}
        </time>
      </div>
    </div>
  );
}

// Animated "agent is typing" indicator
export function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="rounded-card border border-hr-2 bg-bg-3 px-3 py-2.5">
        <div className="flex items-center gap-1">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="inline-block h-1.5 w-1.5 rounded-full bg-fg-3 animate-pulse-dot"
              style={{ animationDelay: `${i * 0.2}s` }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
