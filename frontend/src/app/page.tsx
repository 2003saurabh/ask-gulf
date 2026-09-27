"use client";

import { useCallback, useMemo, useState } from "react";
import ChatPanel from "@/components/ChatPanel";
import TracePanel from "@/components/TracePanel";
import AgentStatusBar, { type AgentStatus } from "@/components/AgentStatusBar";
import type { PanelTraceEntry, TraceEntry } from "@/types";

/**
 * Ask Gulf split-panel page (R12.1, R12.2).
 *
 * Layout: Chat panel occupies the left 70%, Trace panel the right 30%.
 * A header carries the Ask Gulf branding, and an AgentStatusBar shows the
 * current runtime state. ChatPanel owns the WebSocket and forwards incremental
 * `trace` events (with their per-turn id), the `response` timings, and the
 * `response` foreground trace up so this page can accumulate a turn-grouped,
 * deduped trace and render it live in the TracePanel.
 *
 * Trace reconciliation (defect C2): the CRM trace now streams as a LATE
 * `trace` frame AFTER the `response`. So we accumulate live `trace` frames and
 * use the `response.trace` only to reconcile/dedupe — we never reset the panel
 * on a `response`, which would otherwise wipe a subsequently-arriving CRM
 * entry. Entries are grouped by `turn_id` and deduped by (turnId, agent, slot)
 * so a live frame and its echo in `response.trace` render once, and a late CRM
 * frame lands with the correct turn even if the next turn has started.
 */

/** Report-only turn timings surfaced to the trace panel header (R1.1/R1.2). */
export interface Timings {
  routingMs: number;
  totalMs: number;
  turnId?: number;
}

/**
 * Append a live trace entry to the accumulated list, tagged with its turn id
 * and arrival order within that turn. Live frames always append (dedupe only
 * guards the `response.trace` reconcile below).
 */
function appendLiveEntry(
  prev: PanelTraceEntry[],
  entry: TraceEntry,
  turnId?: number,
): PanelTraceEntry[] {
  const order = prev.filter((e) => e.turnId === turnId).length;
  return [...prev, { ...entry, turnId, order }];
}

/**
 * Reconcile the `response.trace` (the foreground entries for a turn) against
 * what has already been accumulated live. For each foreground entry we only
 * add it if the turn does not already hold an entry for that agent at the same
 * ordinal slot — this recovers a live frame that was somehow missed while never
 * duplicating one that already rendered, and never touches a later-arriving
 * CRM entry for the turn.
 */
function reconcileResponseTrace(
  prev: PanelTraceEntry[],
  trace: TraceEntry[],
  turnId?: number,
): PanelTraceEntry[] {
  const forTurn = prev.filter((e) => e.turnId === turnId);
  const additions: PanelTraceEntry[] = [];
  trace.forEach((entry, slot) => {
    const alreadyPresent = forTurn.some(
      (e) => e.order === slot && e.agent_selected === entry.agent_selected,
    );
    if (!alreadyPresent) {
      additions.push({ ...entry, turnId, order: slot });
    }
  });
  if (additions.length === 0) return prev;
  return [...prev, ...additions];
}

export default function Page() {
  const [status, setStatus] = useState<AgentStatus>("connecting");
  const [traceEntries, setTraceEntries] = useState<PanelTraceEntry[]>([]);
  const [timings, setTimings] = useState<Timings | null>(null);

  // A stable per-load session id for the WebSocket path `/ws/{session_id}`.
  const sessionId = useMemo(
    () =>
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `session-${Date.now()}`,
    [],
  );

  const handleTrace = useCallback((entry: TraceEntry, turnId?: number) => {
    setTraceEntries((prev) => appendLiveEntry(prev, entry, turnId));
  }, []);

  const handleResponseTrace = useCallback(
    (trace: TraceEntry[], turnId?: number) => {
      setTraceEntries((prev) => reconcileResponseTrace(prev, trace, turnId));
    },
    [],
  );

  const handleTimings = useCallback(
    (routingMs: number, totalMs: number, turnId?: number) => {
      setTimings({ routingMs, totalMs, turnId });
    },
    [],
  );

  return (
    <main className="flex h-screen flex-col bg-navy text-white">
      <header className="flex items-center gap-3 border-b border-mid bg-navy-dark px-6 py-4">
        <span className="flex h-8 w-8 items-center justify-center rounded-md bg-blue text-sm font-bold text-white">
          AG
        </span>
        <div>
          <h1 className="text-lg font-semibold leading-tight text-white">
            Ask Gulf
          </h1>
          <p className="text-xs text-secondary">
            Agentic business setup assistant
          </p>
        </div>
      </header>

      <AgentStatusBar status={status} />

      <div className="flex min-h-0 flex-1">
        {/* Chat panel — left 70% */}
        <div className="min-h-0 basis-[70%] border-r border-mid">
          <ChatPanel
            sessionId={sessionId}
            onStatusChange={setStatus}
            onTrace={handleTrace}
            onResponseTrace={handleResponseTrace}
            onTimings={handleTimings}
          />
        </div>

        {/* Trace panel — right 30% */}
        <div className="min-h-0 basis-[30%]">
          <TracePanel entries={traceEntries} timings={timings} />
        </div>
      </div>
    </main>
  );
}
