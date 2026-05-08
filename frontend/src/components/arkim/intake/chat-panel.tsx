"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Send, Plus, Package } from "@/components/ui/icons";
import { MessageBubble, TypingIndicator } from "./message-bubble";
import { useArkimStore } from "@/store";
import { useSendMessage, useUploadNameplate } from "@/lib/queries";
import type { ChatMessage } from "@/types";

interface ChatPanelProps {
  runId: string;
  messages: ChatMessage[];
  className?: string;
}

export function ChatPanel({ runId, messages, className }: ChatPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // Optimistic pending messages (cleared when server confirms)
  const [pending, setPending] = useState<ChatMessage[]>([]);
  // Records message count at send time; cleared only when count grows past this baseline
  const pendingBaseRef = useRef(0);

  const chatDraft = useArkimStore((s) => s.chatDraft);
  const setChatDraft = useArkimStore((s) => s.setChatDraft);

  const sendMsg = useSendMessage(runId);
  const uploadFile = useUploadNameplate(runId);

  // Scroll to bottom whenever messages update
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pending, sendMsg.isPending]);

  // Clear pending only when server messages have grown past the baseline set at send time
  useEffect(() => {
    if (pending.length > 0 && messages.length > pendingBaseRef.current) {
      setPending([]);
    }
  }, [messages.length, pending.length]);

  const handleSend = () => {
    const text = chatDraft.trim();
    if (!text || sendMsg.isPending) return;

    setChatDraft("");

    const optimistic: ChatMessage = {
      id: `opt-${Date.now()}`,
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };

    // Snapshot the baseline before appending so the effect above doesn't
    // clear pending until new server messages actually arrive
    pendingBaseRef.current = messages.length;
    setPending((p) => [...p, optimistic]);

    sendMsg.mutate(
      { content: text },
      { onError: () => setPending([]) },
    );
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    uploadFile.mutate(file);
    e.target.value = "";
  };

  const allMessages = [...messages, ...pending];
  const isEmpty = allMessages.length === 0;

  return (
    <div className={cn("flex flex-col h-full", className)}>
      {/* Message history */}
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-2"
      >
        {isEmpty ? (
          <EmptyState />
        ) : (
          <>
            {allMessages.map((msg, i) => (
              <MessageBubble
                key={msg.id}
                message={msg}
                pending={i >= messages.length}
              />
            ))}
            {sendMsg.isPending && <TypingIndicator />}
          </>
        )}
      </div>

      {/* Input area */}
      <div className="shrink-0 border-t border-hr-2 bg-bg-2 p-3">
        <div className="flex items-end gap-2">
          {/* Hidden file input */}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={handleFileChange}
          />

          {/* Attach button */}
          <Button
            variant="ghost"
            size="sm"
            className="shrink-0 self-end"
            loading={uploadFile.isPending}
            onClick={() => fileRef.current?.click()}
            title="Attach nameplate photo"
          >
            <Plus size={13} />
          </Button>

          {/* Text input */}
          <textarea
            ref={inputRef}
            className={cn(
              "flex-1 resize-none rounded border bg-bg-3 px-3 py-2",
              "font-sans text-sm text-fg-1 placeholder:text-fg-4",
              "border-hr-3 focus:border-blue-line focus:outline-none",
              "min-h-[38px] max-h-[120px]",
            )}
            placeholder="Describe the part or paste specs…"
            rows={1}
            value={chatDraft}
            onChange={(e) => {
              setChatDraft(e.target.value);
              // Auto-grow
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 120)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
          />

          {/* Send button */}
          <Button
            variant="primary"
            size="sm"
            className="shrink-0 self-end"
            onClick={handleSend}
            disabled={!chatDraft.trim() || sendMsg.isPending}
          >
            <Send size={13} />
          </Button>
        </div>

        <p className="mt-1.5 font-mono text-[10px] text-fg-4">
          Enter to send · Shift+Enter for new line · 📎 for nameplate photo
        </p>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 py-12 text-center">
      <Package size={28} className="text-fg-4" />
      <div>
        <p className="text-sm text-fg-2 font-medium">What are you sourcing?</p>
        <p className="mt-1 text-[12.5px] text-fg-4 max-w-[260px]">
          Describe the part, paste a model number, or attach a nameplate photo.
        </p>
      </div>
    </div>
  );
}
