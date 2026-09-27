import type { Message } from "@/types";

/**
 * MessageBubble — renders a single chat message with user/assistant styling.
 * User messages align right on the accent blue; assistant messages align left
 * on the card surface (GulfKloud theme, R12.4).
 */
interface MessageBubbleProps {
  message: Message;
}

export default function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div
      className={`flex w-full ${isUser ? "justify-end" : "justify-start"}`}
      data-role={message.role}
    >
      <div
        className={[
          "max-w-[80%] whitespace-pre-wrap break-words rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
          isUser
            ? "bg-blue text-white rounded-br-sm"
            : "bg-card text-white rounded-bl-sm",
        ].join(" ")}
      >
        <div className="mb-1 text-xs font-medium text-secondary">
          {isUser ? "You" : "Ask Gulf"}
        </div>
        {message.text}
      </div>
    </div>
  );
}
