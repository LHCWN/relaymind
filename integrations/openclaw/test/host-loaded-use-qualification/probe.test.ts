// RM16/RM37/RM39; AC28/AC30: candidate probe, never production qualification.
import "openclaw/plugin-sdk/provider-transport-runtime";
import { createOpenAICompletionsTransportStreamFn } from "@openclaw/ai/transports";
import { readFileSync } from "node:fs";
import { expect, it, vi } from "vitest";
import { relaymindModelEntry } from "../../src/compat.ts";
// @ts-expect-error test-only native module
import { createRetainedBoundaryProbe, requireLibrarySuccess } from "./probe.mjs";
// @ts-expect-error recorder is a native module
import { startRecorder } from "../host-transport/recorder.mjs";

function fixture() {
  const controller = new AbortController();
  const borrow = { signal: controller.signal, assertCurrent: vi.fn(), release: vi.fn(() => controller.abort()) };
  const access = { target: { sessionKey: "s", sessionId: "sid", lifecycleRevision: "rev" }, assertCurrent: vi.fn(), retain: vi.fn(() => borrow) };
  const records: any[] = [];
  let now = 0;
  const probe = createRetainedBoundaryProbe({ access, record: (row: any) => records.push(row), now: () => now, maximumMs: 1000 });
  return { probe, borrow, access, records, controller, advance: () => { now = 1000; } };
}

it("retains the actual borrow and uses it after the original handler closes", async () => {
  const f = fixture();
  f.access.assertCurrent.mockImplementation(() => { throw Error("handler closed"); });
  await f.probe.onPayload({ messages: [] }, "sid");
  expect(f.access.retain).toHaveBeenCalledOnce();
  expect(f.records.at(-1).phase).toBe("payload_current");
  f.probe.close(); f.probe.close();
  expect(f.borrow.release).toHaveBeenCalledOnce();
});

it.each(["abort", "expiry", "foreign", "guard"])("refuses %s before delivery and releases once", async (cause) => {
  const f = fixture();
  if (cause === "abort") f.controller.abort();
  if (cause === "expiry") f.advance();
  if (cause === "guard") f.borrow.assertCurrent.mockImplementation(() => { throw Error("private detail"); });
  await expect(f.probe.onPayload({}, cause === "foreign" ? "other" : "sid")).rejects.toThrow("retained_probe_refused");
  expect(JSON.stringify(f.records)).not.toContain("private detail");
  expect(f.borrow.release).toHaveBeenCalledOnce();
});

it("rechecks after an awaited library observation and records failure without inventing a result", async () => {
  const f = fixture();
  await expect(f.probe.observeLibrary(async () => { f.controller.abort(); return { ok: true }; })).rejects.toThrow("retained_probe_refused");
  expect(f.records.some(row => row.phase === "library_observed")).toBe(false);
});

it("the pinned real transport sends zero HTTP requests when onPayload refuses", async () => {
  const replyFixture = JSON.parse(readFileSync(new URL("../fixtures/v2026.10.7/transport/text-streamed.json", import.meta.url), "utf8"));
  const body = replyFixture.exchanges[0].response.body.sse.map((row: unknown) => `data: ${typeof row === "string" ? row : JSON.stringify(row)}\n\n`).join("");
  const recorder = await startRecorder({ replies: [{ status: 200, headers: { "content-type": "text/event-stream" }, body }] });
  const f = fixture();
  try {
    const stream = createOpenAICompletionsTransportStreamFn();
    const model = { ...relaymindModelEntry({ baseUrl: `${recorder.origin}/v1` }), api: "openai-completions", provider: "relaymind", cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } };
    const events = await stream(model as never, { messages: [{ role: "user", content: "fixture", timestamp: 1 }] }, { apiKey: "fixture-token", onPayload: payload => f.probe.onPayload(payload, "foreign") });
    for await (const _event of events) { /* drain actual transport */ }
    expect((await events.result()).stopReason).toBe("error");
    expect(recorder.exchanges).toHaveLength(0);
    expect(f.records.at(-1).phase).toBe("refused");
    // Positive transport control: same pinned model and endpoint really send.
    const positive = await stream(model as never, { messages: [{ role: "user", content: "fixture", timestamp: 1 }] }, { apiKey: "fixture-token" });
    for await (const _event of positive) { /* drain */ }
    expect((await positive.result()).stopReason).toBe("stop");
    expect(recorder.exchanges).toHaveLength(1);
  } finally { f.probe.close(); await recorder.close(); }
});

// RM39/AC30: a returned failure envelope is not a successful library read.
it.each([undefined, null, {}, { ok: false }, { ok: "true" }])("refuses unsuccessful library envelope %j", result => {
  expect(() => requireLibrarySuccess(result)).toThrow("retained_probe_library_refused");
});
it("accepts only an explicit successful library envelope", () => {
  const result = { ok: true, payload: {} };
  expect(requireLibrarySuccess(result)).toBe(result);
});
