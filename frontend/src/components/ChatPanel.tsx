"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import MessageBubble from "@/components/MessageBubble";
import type {
  Message,
  ServerEvent,
  TraceEntry,
  ClientMessage,
} from "@/types";
import type { AgentStatus } from "@/components/AgentStatusBar";

/**
 * ChatPanel (R11.1, R12.1) — owns the WebSocket connection to
 * `/ws/{session_id}`, renders the message list and the input box.
 *
 * Protocol (design §8, updated by the latency-trace fix):
 *  - client sends `{ "text": "..." }`
 *  - server sends, per turn (each frame tagged with a per-turn `turn_id`):
 *      - an optional `{"type":"status","state":"processing","turn_id":N}`
 *      - incremental `{"type":"trace","entry":{...},"turn_id":N}` frames for
 *        the primary/chain agents,
 *      - the `{"type":"response","message":..,"trace":[..],"routing_ms":int,
 *        "total_ms":int,"turn_id":N}` frame, and THEN
 *      - for a recommendation, a LATE `{"type":"trace","entry":{crm_agent..},
 *        "turn_id":N}` frame that arrives AFTER the response (defect C2).
 *
 * ChatPanel consumes `response` events for the message list. Trace frames
 * (including the late CRM one) are forwarded to the parent via `onTrace` with
 * their `turn_id` so the TracePanel can accumulate and reconcile them by turn.
 * The `response` event's `trace` is forwarded via `onResponseTrace` only to
 * reconcile/dedupe against the live entries — it does NOT reset the panel, so a
 * subsequently-arriving CRM entry is never dropped. Timings are forwarded via
 * `onTimings`. Unknown/other frames (e.g. `status`) are ignored gracefully.
 */

/** Resolve the WebSocket base URL, allowing override via env. */
function resolveWsBase(): string {
  const explicit = process.env.NEXT_PUBLIC_WS_BASE_URL;
  if (explicit) return explicit.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    // Default backend dev port.
    return `${proto}://${window.location.hostname}:8000`;
  }
  return "ws://localhost:8000";
}

interface ChatPanelProps {
  sessionId: string;
  onStatusChange?: (status: AgentStatus) => void;
  /** Called for each incremental `trace` event, with its per-turn id. */
  onTrace?: (entry: TraceEntry, turnId?: number) => void;
  /**
   * Called with the `response` event's foreground trace so the panel can
   * reconcile/dedupe it against the live `trace` entries for the same turn.
   * This must NOT wipe entries that arrive later (e.g. the CRM trace).
   */
  onResponseTrace?: (trace: TraceEntry[], turnId?: number) => void;
  /** Called with the report-only timings from a `response` event. */
  onTimings?: (routingMs: number, totalMs: number, turnId?: number) => void;
}

export default function ChatPanel({
  sessionId,
  onStatusChange,
  onTrace,
  onResponseTrace,
  onTimings,
}: ChatPanelProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [awaitingResponse, setAwaitingResponse] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const listEndRef = useRef<HTMLDivElement | null>(null);

  const setStatus = useCallback(
    (status: AgentStatus) => onStatusChange?.(status),
    [onStatusChange],
  );

  // Establish and tear down the WebSocket for this session.
  useEffect(() => {
    const url = `${resolveWsBase()}/ws/${sessionId}`;
    setStatus("connecting");

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setStatus("ready");
    };

    ws.onmessage = (event: MessageEvent<string>) => {
      let data: ServerEvent;
      try {
        data = JSON.parse(event.data) as ServerEvent;
      } catch {
        return; // Ignore malformed frames.
      }

      // Live trace frames (primary/chain agents AND the late CRM entry) are
      // accumulated by the panel, keyed by turn (R11.4, C2).
      if (data.type === "trace") {
        onTrace?.(data.entry, data.turn_id);
        return;
      }

      if (data.type === "response") {
        // Surface the report-only timings (R1.1/R1.2).
        onTimings?.(data.routing_ms, data.total_ms, data.turn_id);
        // Reconcile the panel with the response's foreground trace WITHOUT
        // resetting: this dedupes against live entries but never drops a
        // CRM entry that streams in afterwards (defect C2).
        onResponseTrace?.(data.trace, data.turn_id);
        setMessages((prev) => [
          ...prev,
          {
            id: `assistant-${Date.now()}-${prev.length}`,
            role: "assistant",
            text: data.message,
          },
        ]);
        setAwaitingResponse(false);
        setStatus("ready");
        return;
      }

      // `status` and any unknown frame types are ignored gracefully so the
      // connection stays open (3.5).
    };

    ws.onclose = () => {
      setConnected(false);
      setAwaitingResponse(false);
      setStatus("disconnected");
    };

    ws.onerror = () => {
      setStatus("disconnected");
    };

    return () => {
      wsRef.current = null;
      ws.close();
    };
    // sessionId identifies the connection; callbacks are stable enough for a demo.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  // Keep the latest message in view.
  useEffect(() => {
    listEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = useCallback(() => {
    const text = input.trim();
    const ws = wsRef.current;
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;

    const payload: ClientMessage = { text };
    ws.send(JSON.stringify(payload));

    setMessages((prev) => [
      ...prev,
      { id: `user-${Date.now()}-${prev.length}`, role: "user", text },
    ]);
    setInput("");
    setAwaitingResponse(true);
    setStatus("thinking");
  }, [input, setStatus]);

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    sendMessage();
  };

  const canSend = connected && !awaitingResponse && input.trim().length > 0;

  return (
    <section className="flex h-full min-h-0 flex-col bg-navy" aria-label="Chat">
      <div className="flex-1 min-h-0 space-y-4 overflow-y-auto px-6 py-4">
        {messages.length === 0 ? (
          <p className="mt-8 text-center text-sm text-secondary">
            Ask about licensing, applications, compliance, or booking a meeting
            to get started.
          </p>
        ) : (
          messages.map((message) => (
            <MessageBubble key={message.id} message={message} />
          ))
        )}
        {awaitingResponse && (
          <div className="text-sm text-secondary" aria-live="polite">
            Ask Gulf is thinking…
          </div>
        )}
        <div ref={listEndRef} />
      </div>

      <form
        onSubmit={onSubmit}
        className="flex items-center gap-2 border-t border-mid bg-mid px-6 py-4"
      >
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={
            connected ? "Type your message…" : "Connecting to Ask Gulf…"
          }
          disabled={!connected}
          className="flex-1 rounded-lg border border-card bg-navy px-4 py-2.5 text-sm text-white placeholder:text-secondary focus:border-blue focus:outline-none disabled:opacity-60"
          aria-label="Message input"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="rounded-lg bg-blue px-5 py-2.5 text-sm font-medium text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </section>
  );
}
