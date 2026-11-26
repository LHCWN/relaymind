"""Answer publication, draft checks, and live-stream persistence (RM14, RM20, RM31, RM32; #296)."""

import asyncio
from collections.abc import Callable, Mapping, Sequence

from relaymind.adapters import Message, Role
from relaymind.orchestrator._run_types import (
    ANSWER_LANGUAGE,
    FINAL_INSTRUCTION,
    LOCAL_OPTIONS,
    ConfiguredModel,
    Escalation,
    _Generation,
    _PublicationFailure,
    _RunHost,
)
from relaymind.orchestrator.budgets import BudgetExceededError
from relaymind.orchestrator.handoff import LOCAL_ACTOR
from relaymind.orchestrator.performance import (
    draft_in_language,
    holds_tool_call_json,
    non_default_answer_options,
)
from relaymind.orchestrator.run_state import TERMINAL_STATES
from relaymind.skills.predicates import Predicate
from relaymind.store.run_store import EventDraft
from relaymind.store.trace_event import EventType
from relaymind.tools.registry import ToolResult


class _PublishMixin(_RunHost):
    """Final answer generation, draft checking, and append-only publication mixed into _Run."""

    def _keep_draft(self, text: str) -> None:
        """Keep a rejected local answer so a handoff can label it a proposal (RM13)."""
        if text.strip():
            self.drafts.append(
                {"kind": "answer_draft", "label": "proposal", "text": self.scrub(text)}
            )

    def _repair_or_stop(self, problem: Mapping[str, str]) -> str:
        """The next purpose, repair, if a repair is left; otherwise stop (RM04)."""
        if self.meter.may_repair():
            return "repair"
        raise Escalation("invalid_output", f"unrepaired output: {problem['code']}")

    async def _final(
        self,
        conversation: Sequence[Message],
        predicate: Predicate,
        results: Sequence[ToolResult],
    ) -> str:
        """The final generation with tools disabled (section 7.3)."""
        assert self.language is not None
        language_line = ANSWER_LANGUAGE[self.language]
        _, generation = await self._generate(
            "final",
            (
                *conversation,
                Message(role=Role.SYSTEM, content=f"{FINAL_INSTRUCTION}\n{language_line}"),
            ),
            (),
            self._answer_options(LOCAL_OPTIONS),
            answer_predicate=None if self.skill is None else self.skill.completion_predicate,
        )
        if generation.calls:
            raise Escalation("invalid_output", "the final generation proposed a tool call")
        answer = generation.text
        if generation.rejected or not answer.strip() or not predicate(answer, results):
            self._keep_draft(answer)
            raise Escalation(
                "invalid_output", "the final answer does not meet the completion criteria"
            )
        return self._publish(generation, LOCAL_ACTOR)

    def _draft_publishable(self, draft: str) -> bool:
        """Whether a draft that met the predicate may be the answer (RM31, AC17).

        Only under answer_pass draft. The caller must have sent no
        non-default answer option (section 7.1), because such an option
        shapes the final generation and the recorded answer_options must
        be what shaped the answer (a native run never sets any); an
        option at its API default does not block the draft. The draft
        must be in the run's language and hold no tool-call JSON.
        Otherwise the final generation runs as before.
        """
        if self.o.performance.answer_pass != "draft":
            return False
        if non_default_answer_options(self._answer_option_record()):
            return False
        assert self.language is not None
        return draft_in_language(draft, self.language) and not holds_tool_call_json(draft)

    def _publication_ready(self, external: bool) -> None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        record = self.o.machine.get(self.run_id)
        if record.state in TERMINAL_STATES or record.version != self.record.version:
            raise asyncio.CancelledError
        self.meter.check_time(external)

    def _publish_live(
        self,
        generation: _Generation,
        piece: str,
        model: ConfiguredModel,
        check_deadline: Callable[[], None],
        *,
        dispatch_role: str,
    ) -> None:
        if not piece:
            return
        identity = (generation.call_id, model.model_id, dispatch_role)
        if self.authority.answer_generation not in (None, identity):
            raise _PublicationFailure("answer generation changed")
        try:
            # Serialize the version/state check with append against other store users.
            # The transaction must commit before in-memory ownership is claimed.
            with self.o.store.transaction():
                check_deadline()
                self.authority.require(dispatch_role)
                self.o.store.append_event(
                    self.run_id,
                    EventType.ANSWER_DELTA,
                    model.actor,
                    {"text": piece, "model": model.model_id, "call_id": generation.call_id},
                )
        except (BudgetExceededError, TimeoutError):
            raise
        except Exception as exc:
            raise _PublicationFailure from exc
        # No await separates persistence, ownership and transport visibility.
        self.authority.claim_final_answer(dispatch_role, generation=identity)
        generation.published = True

    def _publish(self, generation: _Generation, role: str) -> str:
        """Publish a validated answer as answer.delta events, once (RM14, RM20).

        role is the dispatch role of the model, local or external; the
        events are labeled with the locality its adapter declares.
        """
        if generation.published:
            return self.scrub(generation.text)
        self.authority.claim_final_answer(role)
        model = self.active_model
        answer = generation.text
        published = self.scrub(answer)
        # A secret may span pieces; then the scrubbed answer is one delta.
        pieces = generation.pieces if published == answer else [published]
        drafts: list[EventDraft] = [
            (EventType.ANSWER_DELTA, model.actor, {"text": piece, "model": model.model_id})
            for piece in pieces
            if piece
        ]
        self.o.store.append_events(self.run_id, drafts)
        generation.published = True
        return published
