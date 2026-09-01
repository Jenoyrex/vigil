import type { SpanOut } from "./api/types";

export interface SpanTreeNode {
  span: SpanOut;
  children: SpanTreeNode[];
  depth: number;
  /** `parent_span_id` is set but wasn't found among the loaded spans --
   * only possible when the trace response is `truncated`. Rendered as a
   * pseudo-root with an indicator, rather than dropped. */
  parentMissing: boolean;
}

/**
 * Builds a span hierarchy from the flat `spans[]` array `GET
 * /v1/traces/{trace_id}` returns, using `parent_span_id` lookups only.
 *
 * The API explicitly documents that array order (`start_time ASC`) does
 * NOT imply parent-before-child -- spans can arrive out of order per ADR
 * 002 -- so this never relies on array position, only on the
 * `parent_span_id` graph.
 *
 * Cycle safety: a span is only ever visited by descending from a root (a
 * span with no parent, or a missing parent). If the input contained a
 * cycle (A's parent is B, B's parent is A), neither A nor B would satisfy
 * "no parent" or "missing parent", so neither becomes a root and the
 * recursive descent below never reaches them -- the cyclic pair is
 * silently excluded rather than causing infinite recursion. This shouldn't
 * occur with a well-behaved SDK, but the function must not crash on
 * malformed data either.
 */
export function buildSpanTree(spans: SpanOut[]): SpanTreeNode[] {
  const byId = new Map(spans.map((span) => [span.span_id, span]));
  const childrenOf = new Map<string, SpanOut[]>();
  const roots: SpanOut[] = [];

  for (const span of spans) {
    const parentId = span.parent_span_id;
    if (parentId === null || !byId.has(parentId)) {
      roots.push(span);
      continue;
    }
    const siblings = childrenOf.get(parentId);
    if (siblings) {
      siblings.push(span);
    } else {
      childrenOf.set(parentId, [span]);
    }
  }

  function build(span: SpanOut, depth: number): SpanTreeNode {
    const children = [...(childrenOf.get(span.span_id) ?? [])]
      .sort((a, b) => a.start_time.localeCompare(b.start_time))
      .map((child) => build(child, depth + 1));
    return {
      span,
      children,
      depth,
      parentMissing: span.parent_span_id !== null && !byId.has(span.parent_span_id),
    };
  }

  return [...roots]
    .sort((a, b) => a.start_time.localeCompare(b.start_time))
    .map((root) => build(root, 0));
}

/** Depth-first, pre-order flattening for rendering the tree as table rows. */
export function flattenSpanTree(nodes: SpanTreeNode[]): SpanTreeNode[] {
  const result: SpanTreeNode[] = [];
  const visit = (node: SpanTreeNode): void => {
    result.push(node);
    for (const child of node.children) visit(child);
  };
  for (const node of nodes) visit(node);
  return result;
}

/** Finds one span_id anywhere in the tree, or null if not present. */
export function findSpanNode(nodes: SpanTreeNode[], spanId: string): SpanTreeNode | null {
  for (const node of nodes) {
    if (node.span.span_id === spanId) return node;
    const found = findSpanNode(node.children, spanId);
    if (found) return found;
  }
  return null;
}

export interface WaterfallPosition {
  leftPercent: number;
  widthPercent: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/**
 * A span's horizontal position/width within the waterfall, as percentages
 * of the trace's total duration. Widths are floored at 0.5% so a very
 * short span (e.g. a fast tool call) still renders a visible sliver rather
 * than disappearing entirely.
 */
export function computeWaterfallPosition(
  span: Pick<SpanOut, "start_time" | "end_time">,
  traceStartMs: number,
  traceDurationMs: number,
): WaterfallPosition {
  if (traceDurationMs <= 0) return { leftPercent: 0, widthPercent: 100 };

  const spanStartMs = new Date(span.start_time).getTime();
  const spanEndMs = new Date(span.end_time).getTime();

  const left = clamp(((spanStartMs - traceStartMs) / traceDurationMs) * 100, 0, 100);
  const rawWidth = ((spanEndMs - spanStartMs) / traceDurationMs) * 100;
  const width = clamp(Math.max(rawWidth, 0.5), 0.5, 100 - left);

  return { leftPercent: left, widthPercent: width };
}
