/**
 * Display formatting only. Nothing here aggregates, sums, or otherwise
 * combines more than one value -- that's ClickHouse's job (see the
 * analytics endpoints); this module exists purely to render already-computed
 * values legibly.
 *
 * `formatCost` in particular never routes the Decimal string through
 * floating-point arithmetic (not even `Number()`), since a monetary value
 * arriving as a string is a deliberate precision guarantee from the API
 * (see docs/decisions/003-clickhouse-telemetry-storage.md section 2) that
 * this module must not quietly undo.
 */

const numberFormatter = new Intl.NumberFormat("en-US");

/** "1,234" -- for span/token/byte counts. */
export function formatCount(value: number): string {
  return numberFormatter.format(value);
}

/** "1,234" or "—" for a possibly-absent count (e.g. an unset token field). */
export function formatNullableCount(value: number | null): string {
  return value === null ? "—" : numberFormatter.format(value);
}

/** 245 -> "245ms", 12500 -> "12.5s", 125000 -> "2.1m", 7200000 -> "2.0h". */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  const abs = Math.abs(ms);
  if (abs < 1000) return `${Math.round(ms)}ms`;

  const seconds = ms / 1000;
  if (Math.abs(seconds) < 60) return `${trimToOneDecimal(seconds)}s`;

  const minutes = seconds / 60;
  if (Math.abs(minutes) < 60) return `${trimToOneDecimal(minutes)}m`;

  const hours = minutes / 60;
  return `${trimToOneDecimal(hours)}h`;
}

function trimToOneDecimal(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

/** "245 B", "12.4 KB", "1.2 MB". */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${trimToOneDecimal(kb)} KB`;
  const mb = kb / 1024;
  return `${trimToOneDecimal(mb)} MB`;
}

/** 0.0234 -> "2.34%". Not a monetary value -- ordinary float formatting is fine. */
export function formatPercent(rate: number, fractionDigits = 2): string {
  if (!Number.isFinite(rate)) return "—";
  return `${(rate * 100).toFixed(fractionDigits)}%`;
}

/**
 * A Decimal64(6) string (e.g. "0.000340", "1.500000") -> "$0.00034",
 * "$1.50". Pure string manipulation -- never parsed through `Number()` or
 * any other floating-point path, so it cannot introduce the rounding error
 * the API's string-typed cost fields exist to avoid.
 */
export function formatCost(value: string | null): string {
  if (value === null) return "—";

  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [whole, fraction = ""] = unsigned.split(".");

  let trimmedFraction = fraction.replace(/0+$/, "");
  if (trimmedFraction.length < 2) {
    trimmedFraction = trimmedFraction.padEnd(2, "0");
  }

  const wholeWithSeparators = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}$${wholeWithSeparators}.${trimmedFraction}`;
}

const relativeTimeFormatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const absoluteTimeFormatter = new Intl.DateTimeFormat("en-US", {
  dateStyle: "medium",
  timeStyle: "medium",
});

/** "3m ago", "2h ago", "5d ago" -- falls back to an absolute date beyond ~30 days. */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";

  const diffSeconds = Math.round((then.getTime() - now.getTime()) / 1000);
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 60) return relativeTimeFormatter.format(diffSeconds, "second");
  if (absSeconds < 3600) return relativeTimeFormatter.format(Math.round(diffSeconds / 60), "minute");
  if (absSeconds < 86400) return relativeTimeFormatter.format(Math.round(diffSeconds / 3600), "hour");
  if (absSeconds < 30 * 86400) return relativeTimeFormatter.format(Math.round(diffSeconds / 86400), "day");

  return absoluteTimeFormatter.format(then);
}

/** Full local date/time, for a title/tooltip attribute. */
export function formatAbsoluteTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return absoluteTimeFormatter.format(date);
}

/** "4bf92f35…e4736" -- visually truncated while keeping the string copyable in full. */
export function truncateId(id: string, headLength = 8, tailLength = 4): string {
  if (id.length <= headLength + tailLength + 1) return id;
  return `${id.slice(0, headLength)}…${id.slice(-tailLength)}`;
}
