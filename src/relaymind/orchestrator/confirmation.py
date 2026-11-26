"""Confirmation and wait-expiry methods for orchestrator runs (RM11, RM17; #296)."""

import hmac
import secrets
from typing import Any

from relaymind.orchestrator._run_types import (
    APPROVAL_EXPIRED,
    GATEWAY_ACTOR,
    INPUT_EXPIRED,
    USER_DENIED,
    ApprovalRejectedError,
    ConfirmationRequest,
    Escalation,
    RunOutcome,
    _arguments_digest,
    _Confirmation,
    _ConfirmationNeeded,
    _digest,
    _finish,
    _RunHost,
    _Step,
)
from relaymind.orchestrator.run_state import CANCEL_REQUESTED, RunState, SlotUnavailableError
from relaymind.store import RunRecord
from relaymind.store.run_store import utc_iso
from relaymind.store.trace_event import EventType


class _ConfirmationMixin(_RunHost):
    """Wait expiry, confirmation issuance, decision, and cancellation mixed into _Run."""

    def _waited_ms(self) -> int:
        assert self.waiting_since is not None
        return round((self.o.clock.monotonic() - self.waiting_since) * 1000)

    def wait_expired(self) -> bool:
        """A clarification wait is over waiting_seconds; a confirmation, past its expiry (RM11)."""
        if self.confirmation is not None:
            return self.o.store.clock.utc_now() >= self.confirmation.expires_at
        assert self.waiting_since is not None
        return self.o.clock.monotonic() - self.waiting_since > self.o.budgets.waiting_seconds

    def expire(self) -> RunOutcome:
        """An expired wait ends as failed with input_expired or approval_expired (section 5).

        Section 12.1: the wait counts toward the waiting_ms of run.failed,
        the only waiting field that event has (section 8). The wait is
        closed only once the transition succeeds: if the store fails, the
        run keeps its wait unchanged, so a later sweep can expire it.
        """
        waited_ms = self._waited_ms()
        self.record = self.o.store.get_run(self.run_id)
        self.waiting_ms += waited_ms
        confirming = self.confirmation is not None
        try:
            outcome = (
                self._fail(APPROVAL_EXPIRED, detail="no approval arrived within five minutes")
                if confirming
                else self._fail(INPUT_EXPIRED, detail="no input arrived within the waiting limit")
            )
        except BaseException:
            self.waiting_ms -= waited_ms
            raise
        self.waiting_since = None
        self.confirmation = None
        return outcome

    def _await_confirmation(
        self, needed: _ConfirmationNeeded, stop: Escalation | None
    ) -> RunOutcome:
        """Suspend in waiting_confirmation with a single-use approval (RM11, RM17).

        The approval is bound to the run's version in waiting_confirmation,
        so it is issued after the transition. Only the token's hash is
        stored; the plaintext token goes to the caller in the outcome.
        """
        admitted = needed.admitted
        target_argument = admitted.tool.confirmation_target
        assert target_argument is not None  # _admit rejects a tool without one
        arguments = self.redact_arguments(admitted.arguments)
        # Read from the redacted arguments, so a secret target is never
        # written in plaintext (RM12); the digest still binds its value.
        target = str(arguments[target_argument])
        # Section 11: the stage ends; waiting is not active time.
        self.meter.end_stage()
        token = secrets.token_urlsafe(32)
        store = self.o.store
        # Transition, approval issuance, and confirmation.required commit in one
        # transaction so concurrent run_view readers never observe waiting_confirmation
        # without a matching pending_approval (RM11, RM17, RM21).
        with store.transaction():
            record = self.o.machine.transition(
                self.run_id,
                self.record.version,
                RunState.WAITING_CONFIRMATION,
            )
            version = record.version
            issued = store.create_approval(
                self.run_id,
                token=token,
                tool_name=admitted.tool_name,
                arguments=arguments,
                target=target,
                state_version=version,
            )
            approval_id, expires_at = issued.approval_id, issued.expires_at
            self.emit(
                EventType.CONFIRMATION_REQUIRED,
                GATEWAY_ACTOR,
                {
                    "approval_id": approval_id,
                    "tool_name": admitted.tool_name,
                    "proposal_id": needed.proposal_id,
                    "arguments": arguments,
                    "target": target,
                    "state_version": version,
                    "expires_at": utc_iso(expires_at),
                    "model": self.active_model.model_id,
                },
            )
        self.record = record
        self.confirmation = _Confirmation(
            loop=needed.loop,
            tool_call=needed.tool_call,
            admitted=admitted,
            proposal_id=needed.proposal_id,
            approval_id=approval_id,
            target=target,
            recorded_arguments=arguments,
            state_version=version,
            expires_at=expires_at,
            stop=stop,
            token_digest=_digest(token),
            arguments_digest=_arguments_digest(admitted),
        )
        self.waiting_since = self.o.clock.monotonic()
        return RunOutcome(
            self.run_id,
            RunState.WAITING_CONFIRMATION,
            confirmation=ConfirmationRequest(
                approval_id=approval_id,
                token=token,
                tool_name=admitted.tool_name,
                arguments=arguments,
                target=target,
                state_version=version,
                expires_at=expires_at,
            ),
            decision=self.decision,
        )

    async def decide(self, approval_id: str, token: str, approve: bool) -> RunOutcome:
        """Answer the pending confirmation and continue the run (RM11, section 5)."""
        return await _finish(self.begin_decision(approval_id, token, approve))

    def begin_decision(self, approval_id: str, token: str, approve: bool) -> _Step:
        """Check and apply the answer to the pending confirmation (RM11, section 5).

        Returns the outcome when the answer ended the run, or the
        continuation to await when the run goes on.

        The approval ID and the token are checked first, so a caller
        without the token gets 403 and changes nothing: it cannot expire
        the run or learn about the slot. Then an expired approval fails
        the run, and an approval checks the execution slot (429). The
        approval is consumed in the same store transaction as the resume
        or the cancel, so a resume that fails, for example because another
        process took the slot, keeps the approval (section 5).
        """
        pending = self.confirmation
        assert pending is not None
        if approval_id != pending.approval_id or not hmac.compare_digest(
            _digest(token), pending.token_digest
        ):
            raise ApprovalRejectedError("the approval does not match the pending confirmation")
        if not hmac.compare_digest(_arguments_digest(pending.admitted), pending.arguments_digest):
            # A held call that drifted from the approval is refused, not executed.
            raise ApprovalRejectedError("the held call does not match the approval")
        if self.wait_expired():
            return self.expire()
        if approve and self.o.machine.active_run_id() is not None:
            raise SlotUnavailableError("another run holds the execution slot (section 11)")
        waited_ms = self._waited_ms()
        store = self.o.store
        with store.transaction():
            consumed = store.consume_approval(
                self.run_id,
                token=token,
                tool_name=pending.admitted.tool_name,
                arguments=pending.recorded_arguments,
                target=pending.target,
                state_version=pending.state_version,
            )
            if not consumed:
                raise ApprovalRejectedError("the token does not match the pending confirmation")
            if approve:
                record = self.o.machine.resume(self.run_id, pending.state_version)
            else:
                record = self._deny(pending, waited_ms)
        self.record = record
        self.waiting_ms += waited_ms
        self.waiting_since = None
        self.confirmation = None
        if not approve:
            return RunOutcome(
                self.run_id, RunState.CANCELLED, error_code=USER_DENIED, decision=self.decision
            )
        self._approvals[pending.proposal_id] = pending.approval_id
        # Like a clarification resume: a new stage with the full per-stage
        # limits, inside the entire-run limits (spec 0.10 section 5).
        self.meter.new_stage()
        if pending.stop is None:
            return lambda: self._protected(lambda: self._run_local(pending.loop, pending))
        stop = pending.stop
        return lambda: self._protected(lambda: self._run_external(pending.loop, stop, pending))

    def cancel_waiting(self, expected_version: int) -> RunRecord:
        """Cancel the run on request while it waits (RM17, RM21).

        The pending approval ends in the same transaction as the
        cancel, and the wait counts toward waiting_ms of run.cancelled.
        """
        waited_ms = self._waited_ms()
        summary = self._summary()
        summary["waiting_ms"] += waited_ms
        payload: dict[str, Any] = {"reason": CANCEL_REQUESTED, **summary}
        if self.confirmation is not None:
            payload["approval_id"] = self.confirmation.approval_id
        store = self.o.store
        with store.transaction():
            store.expire_approvals(self.run_id)
            record = self.o.machine.cancel(
                self.run_id,
                expected_version,
                reason=CANCEL_REQUESTED,
                event=(EventType.RUN_CANCELLED, GATEWAY_ACTOR, payload),
            )
        self.record = record
        self.waiting_ms += waited_ms
        self.waiting_since = None
        self.confirmation = None
        return record

    def _deny(self, pending: _Confirmation, waited_ms: int) -> RunRecord:
        """A denied confirmation ends the run as cancelled with user_denied (section 5)."""
        summary = self._summary()
        summary["waiting_ms"] += waited_ms
        return self.o.machine.cancel(
            self.run_id,
            pending.state_version,
            reason=USER_DENIED,
            event=(
                EventType.RUN_CANCELLED,
                GATEWAY_ACTOR,
                {"reason": USER_DENIED, "approval_id": pending.approval_id, **summary},
            ),
        )
