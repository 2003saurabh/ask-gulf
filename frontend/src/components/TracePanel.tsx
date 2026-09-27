"use client";

import type { PanelTraceEntry } from "@/types";
import type { Timings } from "@/app/page";
import TraceCard from "./TraceCard";

/**
 * TracePanel renders the live orchestration trace: one TraceCard per trace
 * entry accumulated over the WebSocket. The parent (page) owns the WebSocket
 * connection and passes the accumulated, turn-grouped entries down via the
 * `entries` prop, so the panel updates live as each trace frame arrives —
 * including the CRM trace that streams AFTER the response (defect C2).
 *
 * It also surfaces the report-only turn timings (`routing_ms`/`total_ms`) from
 * the latest `response` in a header, without a Trace_Entry (R1.1/R1.2).
 *
 * Requirements:
 *  - 1.1/1.2: report routing time and total turn time alongside the trace.
 *  - 11.3: each card shows agent, routing reason, tools/data stores, latency.
 *  - 11.4: render one card per Trace_Entry when several are produced.
 *  - 12.3: display AWS service badges for Bedrock, DynamoDB, and S3.
 */

// AWS services surfaced as a legend so the audience can see, at a glance,
// which managed services the orchestration touches (R12.3).
const AWS_SERVICES: { key: string; label: string; style: string }[] = [
  { key: "bedrock", label: "Bedrock", style: "bg-blue text-white" },
  { key: "dynamodb", label: "DynamoDB", style: "bg-warning text-navy" },
  { key: "s3", label: "S3", style: "bg-success text-white" },
];

interface TracePanelProps {
  entries: PanelTraceEntry[];
  /** Latest report-only turn timings, or null before the first response. */
  timings?: Timings | null;
}

export default function TracePanel({ entries, timings }: TracePanelProps) {
  return (
    <section
      data-testid="trace-panel"
      className="flex h-full min-h-0 flex-col overflow-y-auto bg-mid px-4 py-4"
      aria-label="Orchestration trace"
    >
      <header className="mb-3 border-b border-white/10 pb-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-secondary">
          Orchestration Trace
        </h2>

        {/* Report-only turn timings (R1.1/R1.2) — not a Trace_Entry. */}
        {timings && (
          <p
            data-testid="trace-timings"
            className="mt-1 text-xs text-secondary"
          >
            Routing:{" "}
            <span data-testid="routing-ms" className="font-medium text-white">
              {timings.routingMs} ms
            </span>{" "}
            ·{" "}Total:{" "}
            <span data-testid="total-ms" className="font-medium text-white">
              {timings.totalMs} ms
            </span>
          </p>
        )}

        {/* AWS service badges (R12.3) */}
        <div data-testid="aws-badges" className="mt-2 flex flex-wrap gap-1.5">
          {AWS_SERVICES.map((svc) => (
            <span
              key={svc.key}
              data-testid={`aws-badge-${svc.key}`}
              data-service={svc.key}
              className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${svc.style}`}
            >
              {svc.label}
            </span>
          ))}
        </div>
      </header>

      <div className="flex-1 space-y-2">
        {entries.length === 0 ? (
          <p data-testid="trace-empty" className="text-xs text-secondary">
            Live agent trace appears here as the orchestrator works.
          </p>
        ) : (
          entries.map((entry, i) => (
            <TraceCard
              key={`${entry.turnId ?? "t"}-${entry.agent_selected}-${entry.order}-${i}`}
              entry={entry}
            />
          ))
        )}
      </div>
    </section>
  );
}
