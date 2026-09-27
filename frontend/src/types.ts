// Shared TypeScript types for the Ask Gulf frontend.
//
// These types are consumed by both Task 16.2 (page / ChatPanel / MessageBubble)
// and Task 16.3 (TracePanel / TraceCard). They mirror the backend contracts
// defined in design §8 (WebSocket Protocol) and the Trace_Entry data model.

/**
 * A single orchestration trace entry, emitted by the runtime per agent
 * invocation. Mirrors the backend `Trace_Entry` (design: Data Models).
 * Every field is required; `time_ms` is a non-negative integer.
 */
export interface TraceEntry {
  agent_selected: string;
  routing_reason: string;
  /** AWS service identifiers, e.g. ["bedrock","dynamodb","s3"]. */
  tools_used: string[];
  /** Elapsed milliseconds for the step; non-negative. */
  time_ms: number;
}

/**
 * A trace entry accumulated in the panel, tagged with the per-turn correlation
 * id so a late CRM trace can be reconciled to the right turn even after the
 * next message has been sent (defect C2). `order` is the arrival index within
 * the turn, used together with `turn_id` + `agent_selected` to dedupe entries
 * that appear both in a live `trace` frame and in the `response.trace`.
 */
export interface PanelTraceEntry extends TraceEntry {
  /** Per-turn correlation id from the backend (`turn_id`). */
  turnId?: number;
  /** Arrival order within the turn; assigned by the panel. */
  order: number;
}

/** A chat message rendered in the ChatPanel message list. */
export interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
}

/**
 * Incremental trace event: `{"type":"trace","entry": Trace_Entry,"turn_id":N}`.
 * Emitted per agent invocation, in real time as each completes. The optional
 * `turn_id` correlates the entry to its turn (additive field; older backends
 * may omit it).
 */
export interface TraceEvent {
  type: "trace";
  entry: TraceEntry;
  /** Per-turn correlation id (additive; may be absent on older backends). */
  turn_id?: number;
}

/**
 * Final response event:
 * `{"type":"response","message":"<text>","trace":[Trace_Entry,...],
 *   "routing_ms":<int>,"total_ms":<int>,"turn_id":N}`.
 *
 * `routing_ms` (R1.1) is how long routing took and `total_ms` (R1.2) is the
 * total time to produce the reply. Both are report-only additive fields (not
 * Trace_Entry records). `turn_id` correlates the response to its turn so a
 * late CRM trace can be reconciled to it.
 */
export interface ResponseEvent {
  type: "response";
  message: string;
  trace: TraceEntry[];
  /** How long routing took, in ms (report-only, R1.1). */
  routing_ms: number;
  /** Total time to produce the reply, in ms (report-only, R1.2). */
  total_ms: number;
  /** Per-turn correlation id (additive; may be absent on older backends). */
  turn_id?: number;
}

/**
 * Status event: `{"type":"status","state":"processing","turn_id":N}`.
 * An additive frame streamed at the start of a turn so the client can show the
 * runtime is working before the first Trace_Entry lands. Consumers that only
 * handle `trace`/`response` simply skip it.
 */
export interface StatusEvent {
  type: "status";
  state: string;
  /** Per-turn correlation id (additive; may be absent on older backends). */
  turn_id?: number;
}

/** Any message the server can send over the WebSocket. */
export type ServerEvent = TraceEvent | ResponseEvent | StatusEvent;

/** The payload the client sends over the WebSocket: `{ "text": "..." }`. */
export interface ClientMessage {
  text: string;
}
