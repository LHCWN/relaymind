// What the guidance plugin reads from disk (RM45, RM47; spec 0.72 section 7.7; review of PR 291).
//
// node:fs is wrapped to count reads, so these tests live apart from guidance-plugin.test.ts.
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import * as fs from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("node:fs", async (importOriginal) => {
  const real = await importOriginal<typeof import("node:fs")>();
  return { ...real, readSync: vi.fn(real.readSync), statSync: vi.fn(real.statSync) };
});

// @ts-expect-error plain JavaScript without declarations
import plugin, { readSkillInfo } from "../guidance-plugin/index.js";

let dir: string;
const readSync = vi.mocked(fs.readSync);
const statSync = vi.mocked(fs.statSync);

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "rm-guidance-reads-"));
  readSync.mockClear();
  statSync.mockClear();
  process.env.RELAYMIND_APP_TOKEN = "app-token";
});
afterEach(() => {
  vi.unstubAllGlobals();
  rmSync(dir, { recursive: true, force: true });
  delete process.env.RELAYMIND_APP_TOKEN;
});

const bytesRead = () => readSync.mock.results.reduce((sum, r) => sum + (r.type === "return" ? Number(r.value) : 0), 0);

function write(name: string, text: string): string {
  const folder = join(dir, name);
  mkdirSync(folder, { recursive: true });
  const path = join(folder, "SKILL.md");
  writeFileSync(path, text);
  return path;
}

describe("reading skill files", () => {
  it("reads only the frontmatter of a hidden skill", () => {
    const path = write("hidden", "---\nname: hidden\ndescription: Hidden\ndisable-model-invocation: true\n---\n" + "x".repeat(200_000));
    expect(readSkillInfo({ name: "hidden", source: { filePath: path } }).hidden).toBe(true);
    expect(bytesRead()).toBeLessThanOrEqual(32_768);
  });

  it("still reads and hashes the whole file of an offered skill", () => {
    const text = "---\nname: big\ndescription: Big\n---\n" + "x".repeat(200_000);
    const path = write("big", text);
    const info = readSkillInfo({ name: "big", source: { filePath: path } });
    expect(info.file.size_bytes).toBe(Buffer.byteLength(text));
  });

  it("keeps a skill hidden when its frontmatter grew past the read limit after stat", () => {
    // RM45, RM47: the bytes read can span a newer version than the reported size.
    const path = write("growing", "---\ndescription: Visible\n---\n");
    const initial = statSync(path);
    writeFileSync(path, "---\ndescription: Hidden\n#" + "x".repeat(32_768) + "\ndisable-model-invocation: true\n---\n");
    statSync.mockReturnValueOnce(initial);
    expect(readSkillInfo({ name: "growing", source: { filePath: path } }).hidden).toBe(true);
    expect(bytesRead()).toBeLessThanOrEqual(32_768);
  });
  it("reads no more skill files once it has the asset limit", async () => {
    const path = write("shared", "---\nname: shared\ndescription: Shared\n---\n");
    // 20,000 is the gateway's asset limit; every entry names its own skill on one shared file.
    const skills = Array.from({ length: 20_010 }, (_, i) => ({ name: `s${i}`, source: { filePath: path, fileHost: "gateway" } }));
    const handlers: Record<string, Function> = {};
    plugin.register({
      pluginConfig: { baseUrl: "http://127.0.0.1:8080/v1" },
      config: { skills: { limits: { maxSkillsInPrompt: 0, maxSkillsLoadedPerSource: 100_000 } } },
      logger: { info() {}, warn() {}, error() {}, debug() {} },
      on: (name: string, handler: Function) => {
        handlers[name] = handler;
      },
      runtime: { agent: { session: { getSessionEntry: async () => ({ skillsSnapshot: { skills } }) } } },
    });
    let offered = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        offered = JSON.parse(String(init.body)).assets.length;
        return new Response(JSON.stringify({ picks: [] }), { status: 200 });
      }),
    );
    statSync.mockClear();
    await handlers.before_prompt_build({ prompt: "merge two pdf files", messages: [] }, { agentId: "main", sessionKey: "k", trigger: "user" });
    expect(offered).toBe(20_000);
    expect(statSync.mock.calls.filter(([p]) => p === path)).toHaveLength(20_000);
  });
});
