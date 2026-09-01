/**
 * Client-side pagination history for GET /v1/traces.
 *
 * The API only returns a forward `next_cursor` -- keyset pagination has no
 * `prev_cursor` (see apps/api/README.md, "Trace list pagination"). "Previous"
 * is implemented entirely client-side as a stack of cursors seen so far.
 * Pure/immutable so it composes naturally with React state (`useState`).
 */

export type CursorStack = readonly (string | null)[];

/** The first page has no cursor. */
export const INITIAL_CURSOR_STACK: CursorStack = [null];

export function currentCursor(stack: CursorStack): string | null {
  return stack.length > 0 ? stack[stack.length - 1] : null;
}

export function canGoBack(stack: CursorStack): boolean {
  return stack.length > 1;
}

export function pushCursor(stack: CursorStack, nextCursor: string): CursorStack {
  return [...stack, nextCursor];
}

export function popCursor(stack: CursorStack): CursorStack {
  return stack.length > 1 ? stack.slice(0, -1) : stack;
}
