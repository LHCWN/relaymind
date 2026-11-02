# Proposed negative disposition of loaded use on the pinned native composition

RM16/RM37/RM39; AC28/AC30; specification 0.68 sections 1, 7.6.1 and 12.6.6.
This is a **pending negative claim**, not an approved host limitation. The
record has no machine review until two actual reviews and their original
outputs are recorded. Loaded use remains disabled throughout.

## Scope of the claim

`stop_point_absent` means absence of a supported point that enforces the
**complete required fence**, not absence of a point that can throw or stop HTTP.
The claim covers the shipped OpenClaw 2026.10.7 native host/provider composition
and supported public integration surfaces, under RelayMind's existing scope.
It does not claim that no arbitrary program, replacement executor, patched
host or later host version could implement a safe fence.

The frozen scope excerpts name the normative boundaries: section 1 excludes a
new executor; RM39 requires the native baseline and unchanged provider behavior;
section 7.6.1 requires current nonremoved library state as well as authority and
immutable bytes immediately before delivery, and preserves native ownership
and baseline behavior. A full replacement harness is therefore outside this
contract. A thin supported wrapper around the builtin attempt would still be
relevant; the public SDK search found no such delegation/accessor.

## Why the earlier withdrawal was correct, and what changed

The earlier host-surface search discovered `retain()` and correctly withdrew
its pending negative claim. There had been no actual composition test, so the
candidate could not be dismissed. Its immutable inventory remains unchanged.

The new VPS diagnostic exercised that candidate. For positive, stale-head and
removed-entry sessions, genuine native ingress retained the access borrow.
The provider found that exact in-process borrow by session ID, and its live
assertion passed after the earlier RPC ended. This proves a useful retained
lifetime and a real provider stop point. It does not independently prove the
identity of the receiving turn's person.

At the provider boundary, the public authenticated dispatcher refused
`skills.library.list` with `authenticated_request_scope_unavailable`. The
retained borrow exposes `signal`, `assertCurrent`, and `release`; it does not
restore a closed authenticated request scope or supply library access. Each
case recorded zero receiving model requests; subsequent native retries were
also refused. The earlier native captures establish why a current library
check is necessary: removal does not prevent the host from retaining and
sending an immutable revision's bytes.

The diagnostic exited 2 because the positive loaded-byte observation was
intentionally stopped. Reset was `control_refused`, and revocation while
waiting was not run. Neither is presented as passing negative-control
qualification. They are not needed to demonstrate this already-missing
required library-currentness link; they would remain required for a positive
qualification.

## Supported alternatives examined

The original hook/middleware/provider inventory and the new closure inventory
are complementary. Hooks and result middleware lack a live native library
entry capability. Public session readers expose session/pin state, which does
not establish a nonremoved library entry. Public skill-command and workspace
readers expose workspace resources, not the personal library's current entry
or tombstone contract. Private backing-store reads and synthesized request
scopes are not supported authority or currentness contracts.

The public custom-harness attempt carries `skillLibraryAuthoring`, so that
capability was not ignored. `registerAgentHarness` requires a replacement
`runAttempt`; its public SDK exports provide no discovered builtin-attempt
wrapper/getter. `runtime.agent.runEmbeddedAgent` admits another run and excludes
`admittedRunContext`/`preparedRunAdmission` from its public input; it is not a
same-attempt delegation. Implementing a new harness would be a new executor,
which section 1 excludes. This is a scope exclusion, not an assertion that the
capability is broken or every custom implementation is impossible.

## Proposed disposition

Within that pinned supported native composition, no examined supported route
combines the real stop/current-authority point with the required fresh native
library-state check. The proposed record therefore uses `stop_point_absent`
and `disabled:true`. Approval requires two recorded reviews, including
OpenCode2, which must challenge this scope interpretation and search for any
missed supported **in-scope** route. A discovered route, incomplete evidence or
unresolved material finding prevents approval. No production registry entry is
added. A new host version reopens the qualification question.

The machine identity was observed directly on this VPS while preparing the
record; it is not a retrospective timing observation. The capture's checkout
and diagnostic source hashes identify its execution. The release's 54 built
files were byte-compared with the captured tarball before deriving the adapter
revision; the environment file records that check. No machine, host run,
reviewer, model or approval identity is invented.
