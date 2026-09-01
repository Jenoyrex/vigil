"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { SpanAnalyticsBucket, SpanBucket } from "@/lib/api/types";
import { formatAbsoluteTime, formatCount } from "@/lib/format";

/**
 * The one place Recharts is used in the app (per the approved design's
 * "justified only here" decision) -- reused by both Overview's compact
 * trend and Analytics' "Over time" mode.
 */
export function TimeSeriesChart({ buckets, bucket }: { buckets: SpanAnalyticsBucket[]; bucket: SpanBucket }) {
  const data = buckets.map((entry) => ({
    time: entry.bucket_start,
    spans: entry.span_count,
    errors: entry.error_span_count,
  }));

  const tickFormatter = (value: string): string => {
    const date = new Date(value);
    return bucket === "hour"
      ? date.toLocaleTimeString([], { hour: "2-digit" })
      : date.toLocaleDateString([], { month: "short", day: "numeric" });
  };

  return (
    <div>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--color-border)" />
          <XAxis dataKey="time" tickFormatter={tickFormatter} stroke="var(--color-muted)" fontSize={11} />
          <YAxis stroke="var(--color-muted)" fontSize={11} allowDecimals={false} width={36} />
          <Tooltip
            labelFormatter={(label) => (typeof label === "string" ? formatAbsoluteTime(label) : String(label ?? ""))}
            formatter={(value, name) => [
              formatCount(typeof value === "number" ? value : Number(value ?? 0)),
              name === "spans" ? "Spans" : "Errors",
            ]}
            contentStyle={{
              background: "var(--color-surface)",
              border: "1px solid var(--color-border)",
              borderRadius: 6,
              fontSize: 12,
            }}
          />
          <Bar dataKey="spans" name="spans" fill="var(--color-accent)" radius={[2, 2, 0, 0]} />
          <Bar dataKey="errors" name="errors" fill="var(--color-status-error)" radius={[2, 2, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
      <p className="sr-only">
        {data.length === 0
          ? "No data in this time range."
          : `Chart of span and error counts across ${data.length} time buckets, from ${formatAbsoluteTime(
              data[0].time,
            )} to ${formatAbsoluteTime(data[data.length - 1].time)}.`}
      </p>
    </div>
  );
}
