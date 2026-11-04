// RM16/RM37/RM39; AC28/AC30. Qualification-only candidate, not an authorization
// adapter. Call only after genuine native handler admission. No production code
// imports this module, and a successful check never creates a registry entry.
export function createRetainedBoundaryProbe({ access, record, now = () => performance.now(), maximumMs = 10000 }) {
  if (!Number.isSafeInteger(maximumMs) || maximumMs < 1 || maximumMs > 60000 ||
      !access?.target || ![access.target.sessionKey, access.target.sessionId, access.target.lifecycleRevision].every(value => typeof value === "string" && value.length > 0 && Buffer.byteLength(value) <= 256)) {
    throw Error("retained_probe_invalid");
  }
  access.assertCurrent();
  const target = { sessionKey: access.target.sessionKey, sessionId: access.target.sessionId, lifecycleRevision: access.target.lifecycleRevision };
  // retainSession deliberately does not retain the original actor's authority.
  const borrow = access.retain();
  const deadline = now() + maximumMs;
  let closed = false;
  let timer;
  const close = () => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    borrow.release();
  };
  const emit = phase => record({ phase, ...target });
  const current = () => {
    if (closed || now() >= deadline || borrow.signal.aborted) throw Error("retired");
    borrow.assertCurrent();
  };
  const refused = () => {
    try { close(); } finally {
      try { emit("refused"); } catch { /* Never expose capture or native error text. */ }
    }
    throw Error("retained_probe_refused");
  };
  try { current(); emit("retained"); } catch { refused(); }
  timer = setTimeout(close, maximumMs);
  timer.unref();
  return {
    close,
    // Supply an actual supported host library API. A failed dispatch outside
    // the handler proves only that the attempted route failed.
    async observeLibrary(read) {
      try {
        current();
        const result = await read();
        current();
        emit("library_observed");
        return result;
      } catch { refused(); }
    },
    async onPayload(_payload, sessionId) {
      try {
        current();
        if (sessionId !== target.sessionId) throw Error("foreign_session");
        emit("payload_current");
        // No replacement. This measures authority lifetime and stopping; it
        // does NOT certify bytes, library state or the current turn's person.
        return undefined;
      } catch { refused(); }
    },
  };
}

/** A native failure envelope is a refusal, even when dispatch did not throw. */
export function requireLibrarySuccess(result) {
  if (result?.ok !== true) throw Error("retained_probe_library_refused");
  return result;
}
