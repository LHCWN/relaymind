# Retained authority at the provider boundary (#280)

RM16, RM37, RM39; AC28, AC30; spec 7.6.1. This is a deterministic-model
**diagnostic**. The separate reviewed record
[`../m10-negative-loaded-use-openclaw-2026.10.7.json`](../m10-negative-loaded-use-openclaw-2026.10.7.json)
binds it together with the scoped source inventory as a negative qualification
for the pinned native composition. Loaded use remains disabled.

The VPS run used OpenClaw 2026.10.7, an installed RelayMind release and a
separate disposable diagnostic provider. The exact diagnostic source digests
are in the capture's `fixture_plugin_sources`; the checkout identifier alone
does not identify its then-uncommitted diagnostic files. The private original
capture and all failed setup attempts remain outside shared scratch storage.
`summary.json` binds the sanitized capture and inspected installed host sources.

For positive, stale-head and removed-entry sessions, an authenticated native
RPC acquired the real `retain()` borrow. The receiving provider found it by
session ID, and `assertCurrent()` succeeded after that RPC ended. Public
`dispatchGatewayMethod("skills.library.list", ...)` at `onPayload` then threw
because an authenticated plugin request scope was unavailable. The diagnostic
refused the request. Each case recorded **zero receiving model requests**.
The host retried; the closed borrow continued to refuse. Reset was refused by
the host's administrator-scope rule, so this does not test reset invalidation.
The harness exited 2 because its positive loaded-byte observation did not pass;
its `completed` status means the diagnostic ran, not that loaded use qualified.

The probe enforces `ok === true` for a returned library envelope as well as
rejecting throws. This prevents a failed native envelope from authorizing a
request. Ordinary production code and qualification registries are unchanged.

The source search also inspected public workspace/skill-command readers,
provider and middleware contracts, and harness registration/runtime exports.
Workspace readers do not expose live Skill Library removal state; middleware
has no library capability. A genuine `skillLibraryAuthoring` capability exists
on admitted harness attempt parameters. Public harness registration accepts a
replacement harness, and the inspected SDK exports expose no builtin-attempt
getter/delegation wrapper. `runtime.agent.runEmbeddedAgent` admits another run,
not the same prepared attempt. A replacement executor is outside the current
M10 contract. The diagnostic alone did not warrant a negative record; the
subsequent scoped inventory and two recorded reviews establish the missing
required library-state link for the pinned native composition. This does not
claim that every arbitrary replacement executor is impossible.

Future positive qualification would still require the receiving turn's exact
person binding, supported fresh
library-state access at the final boundary, full immutable-byte checks, and
revocation/reset/removal while waiting. Loaded use remains disabled.

Privacy edits replace the home directory with `/home/vps-user`, the hostname
with its SHA-256, and any public interface address with a fixed placeholder.
The original capture digest is retained in the summary. No credentials or
private host configuration are published. Tests verify capture/source digests,
recorded refusals and the documented home-path redaction.

## Recorded review

Two separate OpenCode2 sessions inspected the frozen claim and all ten bound
subjects with `opencode-go/glm-5.3-flash`. Both approved without unresolved
material findings. Final raw transcripts and invocation metadata are retained
in `review-a/` and `review-b/`; earlier transcripts are published as the
redacted copies described below. Each final JSON assessment was obtained
by resuming its own inspected context; these final calls are not additional
independent reviews. Reviewer B needed a second final call because its first
report contained Markdown fences. Original intermediate bytes remain private.
The record binds the actual final prompts, input manifest and raw final outputs;
recorder metadata links each final invocation to its predecessor using the
original hashes. The publication manifest separately binds redacted copies.

The reviews retain these minor limitations: `authenticated_request_scope_unavailable`
is the harness classification of the actual host scope error, not a native host
error code; reset was refused and revocation/removal while waiting were not
positive-qualified; the original private capture is digest-bound but the public
redaction transform cannot be independently replayed without it; and the older
CI capture failed later at its account milestone, so only its complete loaded-use
case bodies support this claim. None of these observations enables loaded use.
The unchanged frozen claim explains their scope; this note does not amend it.

Publication privacy correction (3 November): the earlier `review-a-prompt.txt`,
`review-b-prompt.txt`, and `review-a/output.jsonl` / `review-b/output.jsonl` are
now home-path-redacted public projections. Their original bytes are retained
privately; original and published hashes are in
[`../privacy-publication-20261103.json`](../privacy-publication-20261103.json).
The final prompts and final outputs bound by the qualification record are
unchanged. No findings, claims, reviewer identities or review times changed.
A public clone cannot verify private original bytes. Previously published paths
remain in historical commits; this correction does not rewrite Git history.
