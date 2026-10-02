// TypeScript port of services/evaluator/app/relevance.py (RelevanceEvaluator
// 0.1.0): TF-IDF cosine similarity between a span's input and output, fitted
// on just those two texts, reproducing scikit-learn's TfidfVectorizer defaults
// (lowercase, token_pattern r"(?u)\b\w\w+\b", English stop words, smooth_idf,
// l2 norm) and cosine_similarity. Parity: test/fixtures/relevance-parity.json.

import { pyFloatRepr, pyStrip } from "./pyjson";
import { ENGLISH_STOP_WORDS } from "./stopWords";

export const EVALUATOR_NAME = "relevance";
export const EVALUATOR_VERSION = "0.1.0";
const MODEL_NAME = "tfidf-cosine";
const DEFAULT_THRESHOLD = 0.5;

export interface EvaluationResult {
  evaluator_name: string;
  evaluator_version: string;
  score: number | null;
  label: string;
  explanation: string;
  evaluation_latency_ms: number;
  evaluator_model: string;
}

// Python's unicode \w is letters, numbers and underscore; `\b\w\w+\b` matches
// maximal runs of two or more such characters.
const TOKEN_RE = /[\p{L}\p{N}_]{2,}/gu;

function tokenize(text: string): string[] {
  return (text.toLowerCase().match(TOKEN_RE) ?? []).filter((t) => !ENGLISH_STOP_WORDS.has(t));
}

/** Python's str ordering (by code point), used for sklearn's sorted vocabulary. */
function compareCodePoints(a: string, b: string): number {
  const ia = a[Symbol.iterator]();
  const ib = b[Symbol.iterator]();
  for (;;) {
    const ca = ia.next();
    const cb = ib.next();
    if (ca.done || cb.done) return ca.done && cb.done ? 0 : ca.done ? -1 : 1;
    const diff = ca.value.codePointAt(0)! - cb.value.codePointAt(0)!;
    if (diff !== 0) return diff;
  }
}

function l2Normalize(values: number[]): number[] {
  let sumSquares = 0;
  for (const x of values) sumSquares += x * x;
  const norm = Math.sqrt(sumSquares);
  return norm === 0 ? values : values.map((x) => x / norm);
}

/** Returns null when neither text has any vocabulary (sklearn's "empty vocabulary"). */
function tfidfCosineSimilarity(textA: string, textB: string): number | null {
  const docs = [tokenize(textA), tokenize(textB)];
  const vocabulary = [...new Set([...docs[0], ...docs[1]])].sort(compareCodePoints);
  if (vocabulary.length === 0) return null;
  const counts = docs.map((tokens) => {
    const c = new Map<string, number>();
    for (const t of tokens) c.set(t, (c.get(t) ?? 0) + 1);
    return c;
  });
  const nDocs = 2;
  const vectors = counts.map((c) =>
    vocabulary.map((term) => {
      const tf = c.get(term) ?? 0;
      const df = (counts[0].has(term) ? 1 : 0) + (counts[1].has(term) ? 1 : 0);
      const idf = Math.log((1 + nDocs) / (1 + df)) + 1;
      return tf * idf;
    }),
  );
  // TfidfVectorizer normalizes; cosine_similarity normalizes again, then dots.
  const [a, b] = vectors.map(l2Normalize).map(l2Normalize);
  let dot = 0;
  for (let i = 0; i < vocabulary.length; i++) if (a[i] !== 0 && b[i] !== 0) dot += a[i] * b[i];
  return Math.max(0, Math.min(1, dot));
}

/**
 * Python `format(x, ".Nf")`: the exact binary value rounded half-to-even
 * (JavaScript's toFixed rounds exact ties up instead).
 */
export function pyFixed(x: number, digits: number): string {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, x);
  const bits = view.getBigUint64(0);
  const negative = bits >> 63n === 1n;
  const exponentBits = Number((bits >> 52n) & 0x7ffn);
  let mantissa = bits & ((1n << 52n) - 1n);
  let exponent: number;
  if (exponentBits === 0) exponent = -1074;
  else {
    mantissa |= 1n << 52n;
    exponent = exponentBits - 1075;
  }
  // |x| = mantissa * 2^exponent; scaled = |x| * 10^digits as num/den.
  let num = mantissa * 10n ** BigInt(digits);
  let den = 1n;
  if (exponent >= 0) num <<= BigInt(exponent);
  else den <<= BigInt(-exponent);
  let q = num / den;
  const r = num % den;
  if (r * 2n > den || (r * 2n === den && q % 2n === 1n)) q += 1n;
  const text = q.toString().padStart(digits + 1, "0");
  const body = digits > 0 ? `${text.slice(0, -digits)}.${text.slice(-digits)}` : text;
  return negative ? `-${body}` : body; // Python keeps the sign of -0.0
}

function notEvaluable(start: number, explanation: string): EvaluationResult {
  return {
    evaluator_name: EVALUATOR_NAME,
    evaluator_version: EVALUATOR_VERSION,
    score: null,
    label: "not_evaluable",
    explanation,
    evaluation_latency_ms: performance.now() - start,
    evaluator_model: MODEL_NAME,
  };
}

export class InvalidThresholdError extends Error {}

export function evaluateRelevance(inputText: string, outputText: string, threshold: number | null = null): EvaluationResult {
  const start = performance.now();
  const effective = threshold ?? DEFAULT_THRESHOLD;
  if (!(effective >= 0 && effective <= 1)) {
    throw new InvalidThresholdError(`threshold must be within [0.0, 1.0], got ${pyFloatRepr(effective)}.`);
  }
  const input = pyStrip(inputText);
  const output = pyStrip(outputText);
  if (!input || !output) {
    const missing = [!input && "input_text", !output && "output_text"].filter(Boolean).join(" and ");
    return notEvaluable(start, `Relevance requires non-whitespace text on both sides; ${missing} contained none.`);
  }
  const score = tfidfCosineSimilarity(input, output);
  if (score === null) {
    return notEvaluable(
      start,
      "input_text and output_text contained no comparable vocabulary after removing English stop words " +
        "(e.g. both consisted only of punctuation, numbers, or common stop words).",
    );
  }
  const label = score >= effective ? "relevant" : "not_relevant";
  return {
    evaluator_name: EVALUATOR_NAME,
    evaluator_version: EVALUATOR_VERSION,
    score,
    label,
    explanation:
      `TF-IDF cosine similarity between input and output was ${pyFixed(score, 4)} ` +
      `(range [0.0, 1.0]); threshold=${pyFixed(effective, 4)} -> label='${label}'.`,
    evaluation_latency_ms: performance.now() - start,
    evaluator_model: MODEL_NAME,
  };
}
