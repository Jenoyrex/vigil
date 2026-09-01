import { describe, expect, it } from "vitest";

import type { SpanOut } from "./api/types";
import { buildSpanTree, computeWaterfallPosition, findSpanNode, flattenSpanTree } from "./tree";

function makeSpan(overrides: Partial<SpanOut> & Pick<SpanOut, "span_id">): SpanOut {
  return {
    parent_span_id: null,
    name: `span-${overrides.span_id}`,
    span_type: "function",
    resource: "test-service",
    start_time: "2026-08-31T12:00:00.000Z",
    end_time: "2026-08-31T12:00:01.000Z",
    duration_ms: 1000,
    status: "ok",
    status_message: null,
    input: null,
    input_size_bytes: 0,
    input_truncated: false,
    output: null,
    output_size_bytes: 0,
    output_truncated: false,
    attributes: {},
    attributes_truncated: false,
    events: [],
    events_truncated: false,
    llm_provider: null,
    llm_model: null,
    llm_input_tokens: null,
    llm_output_tokens: null,
    llm_total_tokens: null,
    llm_cost_usd: null,
    environment: "production",
    release: null,
    ...overrides,
  };
}

describe("buildSpanTree", () => {
  it("a span with parent_span_id null is a root", () => {
    const spans = [makeSpan({ span_id: "root", parent_span_id: null })];
    const tree = buildSpanTree(spans);
    expect(tree).toHaveLength(1);
    expect(tree[0].span.span_id).toBe("root");
    expect(tree[0].depth).toBe(0);
    expect(tree[0].parentMissing).toBe(false);
  });

  it("nests a child under its parent via parent_span_id, not array position", () => {
    // Child appears BEFORE its parent in the array -- the tree must still
    // be correct, since the API documents that array order does not imply
    // parent-before-child.
    const spans = [
      makeSpan({ span_id: "child", parent_span_id: "root", start_time: "2026-08-31T12:00:00.500Z" }),
      makeSpan({ span_id: "root", parent_span_id: null }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree).toHaveLength(1);
    expect(tree[0].span.span_id).toBe("root");
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].span.span_id).toBe("child");
    expect(tree[0].children[0].depth).toBe(1);
  });

  it("builds multiple levels of depth correctly", () => {
    const spans = [
      makeSpan({ span_id: "root", parent_span_id: null }),
      makeSpan({ span_id: "mid", parent_span_id: "root" }),
      makeSpan({ span_id: "leaf", parent_span_id: "mid" }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree[0].depth).toBe(0);
    expect(tree[0].children[0].depth).toBe(1);
    expect(tree[0].children[0].children[0].depth).toBe(2);
    expect(tree[0].children[0].children[0].span.span_id).toBe("leaf");
  });

  it("treats a span whose parent_span_id is not in the loaded set as a pseudo-root with parentMissing=true", () => {
    // Only possible when the trace response is truncated (see
    // TraceDetailResponse.truncated).
    const spans = [makeSpan({ span_id: "orphan", parent_span_id: "not-loaded" })];
    const tree = buildSpanTree(spans);
    expect(tree).toHaveLength(1);
    expect(tree[0].span.span_id).toBe("orphan");
    expect(tree[0].parentMissing).toBe(true);
  });

  it("sorts siblings and roots by start_time", () => {
    const spans = [
      makeSpan({ span_id: "root-b", parent_span_id: null, start_time: "2026-08-31T12:00:02.000Z" }),
      makeSpan({ span_id: "root-a", parent_span_id: null, start_time: "2026-08-31T12:00:01.000Z" }),
    ];
    const tree = buildSpanTree(spans);
    expect(tree.map((node) => node.span.span_id)).toEqual(["root-a", "root-b"]);
  });

  it("excludes a cyclic pair rather than infinitely recursing", () => {
    // A's parent is B, B's parent is A -- both present, neither a root.
    const spans = [
      makeSpan({ span_id: "a", parent_span_id: "b" }),
      makeSpan({ span_id: "b", parent_span_id: "a" }),
    ];
    expect(() => buildSpanTree(spans)).not.toThrow();
    expect(buildSpanTree(spans)).toHaveLength(0);
  });

  it("returns an empty array for an empty span list", () => {
    expect(buildSpanTree([])).toEqual([]);
  });
});

describe("flattenSpanTree", () => {
  it("flattens depth-first, pre-order", () => {
    const spans = [
      makeSpan({ span_id: "root", parent_span_id: null }),
      makeSpan({ span_id: "child-a", parent_span_id: "root", start_time: "2026-08-31T12:00:00.100Z" }),
      makeSpan({ span_id: "child-b", parent_span_id: "root", start_time: "2026-08-31T12:00:00.200Z" }),
      makeSpan({ span_id: "grandchild", parent_span_id: "child-a", start_time: "2026-08-31T12:00:00.150Z" }),
    ];
    const flat = flattenSpanTree(buildSpanTree(spans));
    expect(flat.map((node) => node.span.span_id)).toEqual(["root", "child-a", "grandchild", "child-b"]);
  });
});

describe("findSpanNode", () => {
  it("finds a nested span by id", () => {
    const spans = [
      makeSpan({ span_id: "root", parent_span_id: null }),
      makeSpan({ span_id: "child", parent_span_id: "root" }),
    ];
    const tree = buildSpanTree(spans);
    expect(findSpanNode(tree, "child")?.span.span_id).toBe("child");
  });

  it("returns null when the span is not present", () => {
    const tree = buildSpanTree([makeSpan({ span_id: "root", parent_span_id: null })]);
    expect(findSpanNode(tree, "missing")).toBeNull();
  });
});

describe("computeWaterfallPosition", () => {
  it("positions a span at the very start of the trace at leftPercent 0", () => {
    const position = computeWaterfallPosition(
      { start_time: "2026-08-31T12:00:00.000Z", end_time: "2026-08-31T12:00:01.000Z" },
      new Date("2026-08-31T12:00:00.000Z").getTime(),
      10_000,
    );
    expect(position.leftPercent).toBe(0);
    expect(position.widthPercent).toBe(10);
  });

  it("positions a span halfway through the trace at leftPercent 50", () => {
    const position = computeWaterfallPosition(
      { start_time: "2026-08-31T12:00:05.000Z", end_time: "2026-08-31T12:00:06.000Z" },
      new Date("2026-08-31T12:00:00.000Z").getTime(),
      10_000,
    );
    expect(position.leftPercent).toBe(50);
  });

  it("floors width at 0.5% so a very short span stays visible", () => {
    const position = computeWaterfallPosition(
      { start_time: "2026-08-31T12:00:00.000Z", end_time: "2026-08-31T12:00:00.001Z" },
      new Date("2026-08-31T12:00:00.000Z").getTime(),
      10_000,
    );
    expect(position.widthPercent).toBeGreaterThanOrEqual(0.5);
  });

  it("returns a full-width bar when trace duration is zero", () => {
    const position = computeWaterfallPosition(
      { start_time: "2026-08-31T12:00:00.000Z", end_time: "2026-08-31T12:00:00.000Z" },
      new Date("2026-08-31T12:00:00.000Z").getTime(),
      0,
    );
    expect(position).toEqual({ leftPercent: 0, widthPercent: 100 });
  });
});
