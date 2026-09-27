import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";

import { readJson, requiredString } from "./_lib";

function post(body: string, contentType: string): NextRequest {
  return new NextRequest("http://localhost/api/workspace/x", {
    method: "POST",
    headers: { "content-type": contentType },
    body,
  });
}

describe("readJson", () => {
  it("accepts a JSON object", async () => {
    const result = await readJson(post('{"name":"Acme"}', "application/json"));
    expect(result).toEqual({ body: { name: "Acme" } });
  });

  it("rejects text/plain even when the body is valid JSON (cross-site form guard)", async () => {
    const result = await readJson(post('{"name":"Acme"}', "text/plain"));
    expect("error" in result && result.error.status).toBe(400);
  });

  it("rejects arrays and malformed JSON", async () => {
    for (const body of ["[1]", "{nope"]) {
      const result = await readJson(post(body, "application/json"));
      expect("error" in result).toBe(true);
    }
  });
});

describe("requiredString", () => {
  it("trims and rejects blanks and non-strings", () => {
    expect(requiredString({ name: "  Acme " }, "name")).toBe("Acme");
    expect(requiredString({ name: "   " }, "name")).toBeNull();
    expect(requiredString({ name: 5 }, "name")).toBeNull();
  });
});
