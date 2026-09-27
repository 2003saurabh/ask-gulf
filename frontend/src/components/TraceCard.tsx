import type { TraceEntry } from "@/types";

/**
 * TraceCard renders a single Trace_Entry: the selected agent, the step
 * latency (time_ms), the routing reason, and one badge per tool used.
 *
 * Requirements: 11.3 (display agent, routing reason, tools/data stores,
 * step latency), 12.3 (AWS service badges for Bedrock, DynamoDB, S3).
 */

// AWS service tools that get a recognizable branded badge (R12.3). Other
// tools (e.g. "fallback-mode") still render as a neutral badge.
const AWS_BADGE_STYLES: Record<string, string> = {
  bedrock: "bg-blue text-white",
  dynamodb: "bg-warning text-navy",
  s3: "bg-success text-white",
};

const AWS_BADGE_LABELS: Record<string, string> = {
  bedrock: "Bedrock",
  dynamodb: "DynamoDB",
  s3: "S3",
};

function ToolBadge({ tool }: { tool: string }) {
  const key = tool.toLowerCase();
  const style = AWS_BADGE_STYLES[key] ?? "bg-mid text-secondary";
  const label = AWS_BADGE_LABELS[key] ?? tool;
  return (
    <span
      data-testid={`tool-badge-${key}`}
      data-tool={key}
      className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-medium ${style}`}
    >
      {label}
    </span>
  );
}

export interface TraceCardProps {
  entry: TraceEntry;
}

export default function TraceCard({ entry }: TraceCardProps) {
  return (
    <div
      data-testid="trace-card"
      className="rounded-lg bg-card p-3 shadow-sm ring-1 ring-white/5"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-white break-all">
          {entry.agent_selected}
        </span>
        <span
          data-testid="trace-latency"
          className="shrink-0 text-xs font-medium text-secondary"
        >
          {entry.time_ms} ms
        </span>
      </div>

      <p
        data-testid="trace-reason"
        className="mt-1 text-sm text-secondary"
      >
        {entry.routing_reason}
      </p>

      {entry.tools_used.length > 0 && (
        <div
          data-testid="trace-tools"
          className="mt-2 flex flex-wrap gap-1.5"
        >
          {entry.tools_used.map((tool, i) => (
            <ToolBadge key={`${tool}-${i}`} tool={tool} />
          ))}
        </div>
      )}
    </div>
  );
}
