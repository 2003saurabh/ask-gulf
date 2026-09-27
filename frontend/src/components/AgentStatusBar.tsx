"use client";

/**
 * AgentStatusBar (R12.2) — a slim status strip beneath the header that shows
 * the current connection / activity state of the orchestration runtime.
 */

export type AgentStatus = "connecting" | "ready" | "thinking" | "disconnected";

const STATUS_META: Record<
  AgentStatus,
  { label: string; dotClass: string }
> = {
  connecting: { label: "Connecting…", dotClass: "bg-warning" },
  ready: { label: "Ready", dotClass: "bg-success" },
  thinking: { label: "Agents working…", dotClass: "bg-blue animate-pulse" },
  disconnected: { label: "Disconnected", dotClass: "bg-error" },
};

interface AgentStatusBarProps {
  status: AgentStatus;
}

export default function AgentStatusBar({ status }: AgentStatusBarProps) {
  const meta = STATUS_META[status];

  return (
    <div className="flex items-center gap-2 border-b border-mid bg-mid px-6 py-2 text-sm text-secondary">
      <span
        className={`inline-block h-2.5 w-2.5 rounded-full ${meta.dotClass}`}
        aria-hidden="true"
      />
      <span>Orchestrator</span>
      <span className="text-white">{meta.label}</span>
    </div>
  );
}
