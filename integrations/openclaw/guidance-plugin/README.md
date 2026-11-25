# RelayMind guidance plugin for OpenClaw

Optional. On each prompt it asks the RelayMind gateway which few skills the prompt needs, and
adds one labeled hint to that turn. When the gateway asks for it, it also reports what a successful run used. With `limit: "strict"` it also refuses what was offered and not
selected. It grants nothing. If anything fails it adds no hint and no limit, and the turn goes on.
See [the tutorial](../../../docs/guidance.md).

The quickest setup is `relaymind openclaw guidance`, which runs the host commands for everything below. By hand:

```json5
{
  plugins: {
    load: { paths: ["/path/to/relaymind/integrations/openclaw/guidance-plugin"] },
    entries: {
      "relaymind-guidance": {
        enabled: true,
        hooks: { allowConversationAccess: true },   // OpenClaw requires this for the prompt hook
        config: {
          baseUrl: "http://127.0.0.1:8080/v1",       // required; loopback only
          // tokenEnv: "RELAYMIND_APP_TOKEN",        // where the application token is
          // timeoutMs: 13000,                       // 1 to 14000; go on without a hint after this
          // maxPicks: 8,                            // unset: the gateway decides, 8 by default
          // toolsFile: "/abs/path/tools.json",      // optional [{name, description}] tools; each
          //                                         // name as the host calls it: server__tool
          // limit: "strict"                         // default "hint"; see the tutorial
        }
      }
    }
  },
  // Your choice, not the plugin's: empty the host's own listed skill catalog to save context.
  // With many skills, also raise maxSkillsLoadedPerSource (default 200) and maxCandidatesPerRoot
  // (default 300): the host does not load a skill past these caps, so it cannot be selected.
  skills: { limits: { maxSkillsInPrompt: 0 } }
}
```

With `limit: "strict"` the plugin holds, per session, what the current prompt selected and what the
session already used. This is not a security wall: `exec` can still read a skill, a tool outside
`toolsFile` is not limited, and in OpenClaw's code mode `skills.list()` shows every skill without a
tool call, so no hook sees it. For a wall, also remove those tools, and code mode, with OpenClaw's
own tool policy.

Set the gateway's application token in the environment of the OpenClaw process. A running gateway
prints a new token at each start.

It is a separate plugin on purpose. The native qualification pins the provider plugin's built
bytes, so a change there would turn its approved direct actions off until they are qualified again.

For each skill it offers, the plugin sends the name, the description and, for a `SKILL.md` of at most
1 MiB, that file's SHA-256, absolute path and size (spec 0.72). It never sends the body. The gateway passes the
hash on as the skill's `content_hash`, and the path only to a selector that syncs (see
[the tutorial](../../../docs/guidance.md#keep-your-selector-in-sync)). A gateway older than spec 0.72 refuses
these fields with HTTP 400. The plugin then sends the request once more without them, within the same
`timeoutMs`, and logs once that it sends no file fields for the rest of the process. The turn keeps its hint,
but selector sync needs a gateway of 0.72 or later, so upgrade the gateway too and restart the host.

The plugin reads a skill file again only when its size, modification time or change time changed. Of a hidden
skill it reads only the frontmatter, and it reads no more files once it has 20,000 assets, the gateway's limit.
Beyond the first 32 KiB of each file, it reads at most 64 MiB of skill files whole in one turn; a skill past that
is offered by its description and read on a later turn, when its `content_hash` changes once to the file's hash.
When the request would pass the gateway's body limit, it leaves out the file fields first and shortens
descriptions only after that.

Selector feedback (spec 0.75): when the gateway's answer carries a `recommendation_id`, which it does only when
the operator set `feedback_url` for a third-party selector, the plugin records which offered skills that run
loaded (`skills_read`, or a plain `read` inside the skill's folder) and which offered tools it called, counting
only calls that returned without error. When the run ends with success it sends them to the gateway, picked or
not, with no prompt text or path; a failed run sends nothing. It needs no setting of its own, and with no
`recommendation_id` it sends nothing. See [the tutorial](../../../docs/guidance.md#tell-your-selector-what-a-turn-used).

Tools: OpenClaw's plugin API gives a plugin no list of the host's tools, so tools come from the
optional `toolsFile`. `relaymind openclaw guidance --tools-file PATH` writes it from the host's MCP
servers. OpenClaw's Tool Search already keeps MCP tool schemas out of the request by
default, so skills are the main context cost.
