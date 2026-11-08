// The package and plugin manifests (RM33, section 7.5: pinned release, public SDK only).
import { readFileSync, readdirSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { MODEL_ID, PINNED_OPENCLAW, PROVIDER_ID } from "../src/compat.ts";
import { EXECUTION_MODES } from "../src/config.ts";

const root = new URL("../", import.meta.url);
const read = (path: string) => JSON.parse(readFileSync(new URL(path, root), "utf8"));
const manifest = read("openclaw.plugin.json");
const pkg = read("package.json");
const host = read("node_modules/openclaw/package.json");

// What the package declares to the host (spec 0.70, section 7.5). It is kept out of the shipped
// source on purpose: the native adapter revision hashes the shipped JavaScript, so changing it
// would drop the reviewed native qualification. Keep it equal to DECLARED_RANGE in
// src/relaymind/openclaw_setup.py (tests/test_openclaw_setup.py checks that one).
const DECLARED_RANGE = ">=2026.10.7 <2027.0.0-0";

describe("openclaw.plugin.json", () => {
  it("declares the relaymind provider", () => {
    expect(manifest.id).toBe(PROVIDER_ID);
    expect(manifest.providers).toEqual([PROVIDER_ID]);
    expect(typeof manifest.name).toBe("string");
    expect(typeof manifest.description).toBe("string");
  });

  it("declares the token's environment variable and the api-key auth choice", () => {
    expect(manifest.setup.providers).toEqual([{ id: PROVIDER_ID, envVars: ["RELAYMIND_APP_TOKEN"] }]);
    const [choice] = manifest.providerAuthChoices;
    expect(choice.provider).toBe(PROVIDER_ID);
    expect(choice.method).toBe("api-key");
    expect(choice.cliFlag).toBe("--relaymind-app-token");
    expect(manifest.providerAuthChoices).toHaveLength(1);
  });

  it("has a closed config schema that matches the plugin's own checks", () => {
    const schema = manifest.configSchema;
    expect(schema.type).toBe("object");
    expect(schema.additionalProperties).toBe(false);
    expect(schema.required).toEqual(["baseUrl"]);
    expect(Object.keys(schema.properties).sort()).toEqual([
      "baseUrl",
      "diagnosticsPath",
      "executionMode",
      "limits",
      "maxRetries",
      "recommendations",
    ]);
    expect(schema.properties.diagnosticsPath).toMatchObject({ type: "string", minLength: 1 });
    expect(schema.properties.maxRetries).toMatchObject({ type: "integer", minimum: 0, maximum: 2, default: 0 });
    // parallel_demo is not a mode of this alias (section 7.5).
    expect(schema.properties.executionMode.enum).toEqual([...EXECUTION_MODES]);
    expect(schema.properties.executionMode.default).toBe("auto");
    const limits = schema.properties.limits;
    expect(limits.additionalProperties).toBe(false);
    expect(Object.keys(limits.properties).sort()).toEqual([
      "externalContextLimit",
      "localContextLimit",
      "maxCompletionTokens",
    ]);
    for (const limit of Object.values(limits.properties) as { type: string; minimum: number }[]) {
      expect(limit).toMatchObject({ type: "integer", minimum: 1 });
    }
  });

  it("leaves parallel_demo out of the executionMode enum, so the host refuses it first (section 7.5)", () => {
    // The pinned host validates plugins.entries.relaymind.config against this
    // schema before any plugin code runs: `config set ... executionMode
    // parallel_demo` is declined by the host's own validation, and the
    // plugin's own refusal (src/config.ts) is the second line only.
    const mode = manifest.configSchema.properties.executionMode;
    expect(mode.type).toBe("string");
    expect(mode.enum).not.toContain("parallel_demo");
    expect(mode.enum).toEqual(["auto", "local_only", "external_only"]);
  });

  it("names no credential field in the config schema (RM12)", () => {
    expect(JSON.stringify(manifest.configSchema)).not.toMatch(/apiKey|appToken|secret|password/i);
  });
});

describe("package.json", () => {
  it("loads the built entry, not TypeScript source", () => {
    expect(pkg.type).toBe("module");
    expect(pkg.openclaw.extensions).toEqual(["./dist/index.js"]);
    expect(pkg.openclaw.providers).toEqual([PROVIDER_ID]);
    expect(pkg.files).toEqual(expect.arrayContaining(["dist", "openclaw.plugin.json", "README.md"]));
  });

  it("builds against the qualified OpenClaw release and declares a range from it", () => {
    // The build and the captured fixtures stay on the one qualified release.
    expect(pkg.openclaw.build.openclawVersion).toBe(PINNED_OPENCLAW);
    expect(pkg.openclaw.build.pluginSdkVersion).toBe(PINNED_OPENCLAW);
    expect(pkg.devDependencies.openclaw).toBe(PINNED_OPENCLAW);
    expect(pkg.devDependencies["@openclaw/ai"]).toBe(PINNED_OPENCLAW);
    expect(host.version).toBe(PINNED_OPENCLAW);
    // What the host is told (section 7.5, spec 0.70): loadable from the qualified
    // release to the end of the year. Loading is not qualification.
    expect(pkg.openclaw.compat.pluginApi).toBe(DECLARED_RANGE);
    expect(pkg.openclaw.compat.minGatewayVersion).toBe(PINNED_OPENCLAW);
    // The host provides openclaw/plugin-sdk at runtime.
    expect(pkg.peerDependencies.openclaw).toBe(DECLARED_RANGE);
    expect(pkg.engines.node).toBe(host.engines.node);
  });

  it("is accepted by the host's own range check from the qualified release to the end of 2026", async () => {
    // The host's package-compat module decides whether a plugin loads.
    const dist = new URL("node_modules/openclaw/dist/", root);
    const [file] = readdirSync(dist).filter((name) => /^package-compat-.*\.mjs$/.test(name));
    expect(file).toBeDefined();
    const compat = await import(new URL(file!, dist).href);
    const satisfies = compat.n as (version: string, range: string) => boolean;
    const range = pkg.openclaw.compat.pluginApi as string;
    // The qualified release, a numeric correction of it, and later 2026 releases load.
    for (const ok of [
      PINNED_OPENCLAW,
      `${PINNED_OPENCLAW}-1`,
      // The host drops a pre-release suffix before it compares, so it loads these. The install
      // helper follows SemVer and is stricter for a pre-release of the lower bound (refused
      // before any host change); it is never looser than the host.
      `${PINNED_OPENCLAW}-beta.2`,
      "2026.11.1-rc.1",
      "2026.10.8",
      "2026.11.1",
      "2026.12.31",
    ]) {
      expect(satisfies(ok, range), ok).toBe(true);
    }
    // An earlier release and the next year (pre-releases of it too) are refused.
    for (const refused of ["2026.10.6", "2026.10.6-beta.1", "2026.9.30", "2025.12.1", "2027.0.0", "2027.0.0-beta.1", "2027.1.1"]) {
      expect(satisfies(refused, range), refused).toBe(false);
    }
  });

  it("has no runtime dependency the host would have to install", () => {
    expect(pkg.dependencies ?? {}).toEqual({});
  });

  it("has the build, typecheck, test, pack check, and release scripts", () => {
    for (const script of ["build", "typecheck", "test", "pack:check", "pack:release"]) {
      expect(typeof pkg.scripts[script]).toBe("string");
    }
  });
});

describe("source imports (public SDK only)", () => {
  const exported = new Set(Object.keys(host.exports));
  const sources = readdirSync(new URL("src/", root)).filter((f) => f.endsWith(".ts"));

  it.each(sources)("%s imports openclaw only through published plugin-sdk subpaths", (file) => {
    const text = readFileSync(new URL(`src/${file}`, root), "utf8");
    const specifiers = [...text.matchAll(/(?:from|import)\s*\(?\s*(["'])([^"']+)\1/g)].map((m) => m[2]!);
    for (const specifier of specifiers) {
      expect(specifier).not.toMatch(/node_modules|\/dist\//);
      if (specifier === "openclaw" || specifier.startsWith("openclaw/")) {
        expect(specifier.startsWith("openclaw/plugin-sdk/")).toBe(true);
        expect(exported.has(`./${specifier.slice("openclaw/".length)}`)).toBe(true);
      }
      expect(specifier.startsWith("@openclaw/")).toBe(false);
    }
  });

  it("serves the one alias", () => {
    expect(MODEL_ID).toBe("relaymind-client-tools");
  });
});
