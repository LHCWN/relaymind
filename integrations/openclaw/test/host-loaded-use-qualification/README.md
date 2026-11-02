# Retained-borrow boundary probe (#280)

RM16, RM37, RM39; AC28, AC30; specification 7.6.1.

This is qualification scaffolding, excluded from the release package. It does
not register an RPC, authorize a caller, activate a skill, or qualify loaded use.

`createRetainedBoundaryProbe` accepts an already admitted handler's actual
`sessionAccessAuthority`. It calls `retain()` (not `retainSession()`), pins its
session identity, bounds its lifetime to at most 60 seconds, and checks the
borrow at an `onPayload` boundary. The caller must close it when the trial ends.
Its `observeLibrary` callback allows a trial to attempt a supported library read
outside the original handler and check authority before and after the await.
A callback's success is only an observed return; it does not prove that an entry
is current or its bytes valid. The payload probe does not inspect instruction
bytes or establish who owns the receiving turn.

Run the tests from `integrations/openclaw`:

```sh
npm test -- --run test/host-loaded-use-qualification/probe.test.ts
npm run typecheck
```

The transport test uses the installed pinned OpenClaw transport and a real
loopback HTTP recorder. A refused payload yields zero requests; a positive
control sends one request and consumes a scripted response. Authority lifetime
checks use explicitly declared doubles. This is not a real-host retain capture.

To continue qualification, acquire the probe inside genuine authenticated
native ingress and connect its final check to the same host's receiving
provider. Capture whether the borrow survives the handler and identifies the
turn's actual person and session. Attempt supported live library-state access
outside that handler, and record refusals without inventing library state.
Then add exact native tool-result byte checks, current gateway choice and
registration checks, and removal, revocation and reset during a paused turn.
The existing observation harness's `control_refused` reset result is not a
passing reset fence. No production registry entry or negative host-limit record
is justified by these tests alone.

The opt-in `qualify.py` driver extends the ordinary host fixture. It installs a
separate disposable provider and direct RPC from `plugin.mjs`, using the existing
person-admission probe. This is a diagnostic composition, not the released
provider. It records whether the retained borrow is visible by the receiving
provider session ID, checks it in `onPayload`, and attempts the public
`skills.library.list` dispatch there. A refused library dispatch stops the
request; a successful one permits the first request and deliberately refuses
the second to observe the stop. Matching a session ID alone is not treated as
proof of the receiving turn's person. The existing ordinary grader may report
the intentionally stopped observation incomplete.

Run with a freshly built release tarball, a private assigned interface and the
ordinary harness's browser prerequisites. Choose a durable private output
folder outside shared scratch space:

```sh
python integrations/openclaw/test/host-loaded-use-qualification/qualify.py \
  --observe-loaded-use --tarball /absolute/path/to/provider.tgz \
  --work-dir /absolute/private/run --out /absolute/private/capture.json
```

The private capture includes the original fixture messages. Review and redact
host identifiers before publishing it; never publish the fixture's private
configuration or raw host logs. No inference endpoint other than the local
scripted gateway is used by this driver.
