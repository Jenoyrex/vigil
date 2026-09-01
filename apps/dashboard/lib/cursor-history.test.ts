import { describe, expect, it } from "vitest";

import {
  canGoBack,
  currentCursor,
  INITIAL_CURSOR_STACK,
  popCursor,
  pushCursor,
} from "./cursor-history";

describe("cursor history", () => {
  it("starts with a single null cursor (page 1) and cannot go back", () => {
    expect(currentCursor(INITIAL_CURSOR_STACK)).toBeNull();
    expect(canGoBack(INITIAL_CURSOR_STACK)).toBe(false);
  });

  it("pushing a cursor advances the current position and allows going back", () => {
    const afterNext = pushCursor(INITIAL_CURSOR_STACK, "cursor-page-2");
    expect(currentCursor(afterNext)).toBe("cursor-page-2");
    expect(canGoBack(afterNext)).toBe(true);
  });

  it("supports multiple forward pages, each pushed onto the stack", () => {
    let stack = pushCursor(INITIAL_CURSOR_STACK, "page-2");
    stack = pushCursor(stack, "page-3");
    expect(currentCursor(stack)).toBe("page-3");
  });

  it("popping returns to the previous cursor", () => {
    let stack = pushCursor(INITIAL_CURSOR_STACK, "page-2");
    stack = pushCursor(stack, "page-3");
    stack = popCursor(stack);
    expect(currentCursor(stack)).toBe("page-2");
    stack = popCursor(stack);
    expect(currentCursor(stack)).toBeNull();
  });

  it("popping at the first page is a no-op (never goes below the initial cursor)", () => {
    const stack = popCursor(INITIAL_CURSOR_STACK);
    expect(currentCursor(stack)).toBeNull();
    expect(canGoBack(stack)).toBe(false);
  });

  it("push/pop never mutate the input stack (pure/immutable)", () => {
    const original = INITIAL_CURSOR_STACK;
    pushCursor(original, "page-2");
    expect(original).toEqual([null]);
  });
});
