"""Tool admission, prefetch, and execution for orchestrator runs (RM05, RM08-RM10, RM29; #296)."""

import asyncio
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import replace

from relaymind.adapters import ToolCall, ToolSchema
from relaymind.orchestrator import identifiers
from relaymind.orchestrator._run_types import (
    CONFIRMATION_UNSUPPORTED,
    GATEWAY_ACTOR,
    PREFETCH,
    Escalation,
    _ConfirmationNeeded,
    _count,
    _Generation,
    _Loop,
    _tool_exchange,
    _tool_exchange_set,
    _Turn,
    skill_stage_messages,
)
from relaymind.orchestrator.handoff import LOCAL_ACTOR
from relaymind.orchestrator.prefetch import resolve_arguments
from relaymind.orchestrator.route_decision import RouteDecision
from relaymind.skills import Skill
from relaymind.skills.predicates import COMPLETION_PREDICATES
from relaymind.store.trace_event import EventType
from relaymind.tools import ToolRejectedError
from relaymind.tools.executor import (
    GATEWAY_OPERATION_MODEL,
    Execution,
    OperationInFlightError,
    ToolExecutor,
    ValidatedCall,
)
from relaymind.tools.registry import SideEffect, ToolResult, ToolStatus


class _ToolsMixin:
    """Skill loading, tool admission, prefetch, and execution mixed into _Run."""

    def _permitted_tools(
        self, allowlist: Sequence[str]
    ) -> tuple[frozenset[str], tuple[ToolSchema, ...]]:
        """The tools of an allowlist this caller may use, and their schemas (RM08)."""
        available = self.o.tools.available_tools(
            skill_allowlist=allowlist, policy=self.o.policy, caller=self.o.caller
        )
        return available, self.o.tools.tool_schemas(n for n in allowlist if n in available)

    def _skill_allowlist(self, skill: Skill) -> tuple[str, ...]:
        """The skill's allowed tools, less those a selector's narrowing removed (RM08, RM28)."""
        if self.narrowed is None:
            return skill.allowed_tools
        return tuple(tool for tool in skill.allowed_tools if tool in self.narrowed)

    def _load_skill(self, name: str) -> Skill:
        """Load the skill at the version run.accepted recorded (RM06, RM07)."""
        pinned = next(s for s in self.offered["skills_offered"] if s["name"] == name)
        return self.o.skills.load_skill(
            name,
            emit=lambda event_type, payload: self.emit(event_type, GATEWAY_ACTOR, payload),
            version=pinned["version"],
            content_hash=pinned["content_hash"],
        )

    def _local_setup(self, decision: RouteDecision, *, read_only: bool = False) -> _Loop:
        """Load the skill and build the first local conversation (RM06, RM08)."""
        assert decision.skill_id is not None
        skill = self.skill = self._load_skill(decision.skill_id)
        available, schemas = self._permitted_tools(self._skill_allowlist(skill))
        if read_only:
            available = frozenset(
                name
                for name in available
                if self.o.tools.get(name).side_effect is not SideEffect.MUTATING
            )
            schemas = tuple(schema for schema in schemas if schema.name in available)
        if not schemas:
            raise Escalation(
                "capability_missing",
                f"no permitted tools for skill {skill.name!r}",
                hand_off=False,
            )
        conversation = skill_stage_messages(
            skill, self.messages, [schema.name for schema in schemas]
        )
        predicate = COMPLETION_PREDICATES[skill.completion_predicate]
        return _Loop(conversation, schemas, available, skill, predicate)

    async def _prefetch(self, loop: _Loop) -> None:
        """Run the skill's declared calls before its first generation (RM29, AC18)."""
        skill = loop.skill
        if not self.o.performance.prefetch or skill is None or not skill.prefetch:
            return
        user_text = identifiers.provenance_text(self.messages)
        planned: list[tuple[ToolCall, ValidatedCall | None, dict[str, str] | None]] = []
        for index, declared in enumerate(skill.prefetch):
            schema = self.o.tools.get(declared.tool).declaration.input_schema
            arguments = resolve_arguments(declared, schema, user_text)
            if arguments is None:
                continue
            call = ToolCall(
                call_id=f"prefetch_{index + 1}", tool_name=declared.tool, arguments=arguments
            )
            admitted, rejection = self._admit_one(call, loop.available, self.results)
            planned.append((call, admitted, rejection))
        runnable = [(call, admitted) for call, admitted, _ in planned if admitted is not None]
        if self._new_executions(admitted for _, admitted in runnable) > self.meter.tool_room():
            return
        proposal_ids = {
            call.call_id: self._propose(call, skill, rejection, mechanism=PREFETCH)
            for call, _, rejection in planned
        }
        if not runnable:
            return
        outcomes = await asyncio.gather(
            *(
                self._dispatch_step(loop, admitted, proposal_ids[call.call_id], mechanism=PREFETCH)
                for call, admitted in runnable
            ),
            return_exceptions=True,
        )
        failed = next((o for o in outcomes if isinstance(o, BaseException)), None)
        if failed is not None:
            raise failed
        results = [o for o in outcomes if isinstance(o, ToolResult)]
        loop.conversation += _tool_exchange_set(
            [call for call, _ in runnable], [r.to_json_object() for r in results]
        )
        pending = loop.available - {
            r.tool_name for r in self.results if r.status is ToolStatus.SUCCEEDED
        }
        if not pending:
            loop.schemas = ()
            loop.available = frozenset()

    async def _tool_step(self, loop: _Loop, turn: _Turn) -> None:
        """Run an admitted turn, or hold its call for a confirmation if it mutates (RM11)."""
        if len(turn.admitted) > 1:
            await self._execute_set(loop, turn)
            return
        call, admitted, proposal_id = turn.calls[0], turn.admitted[0], turn.proposal_ids[0]
        if admitted.tool.side_effect is SideEffect.MUTATING and not self.o.executor.has_operation(
            admitted
        ):
            raise _ConfirmationNeeded(loop, call, admitted, proposal_id)
        await self._execute_step(loop, call, admitted, proposal_id)

    async def _execute_step(
        self, loop: _Loop, call: ToolCall, admitted: ValidatedCall, proposal_id: str
    ) -> None:
        """Dispatch an admitted call as the loop's actor (RM14) and give its outcome on."""
        result = await self._dispatch_step(loop, admitted, proposal_id)
        loop.conversation += _tool_exchange(call, result.to_json_object())

    async def _dispatch_step(
        self,
        loop: _Loop,
        admitted: ValidatedCall,
        proposal_id: str,
        *,
        mechanism: str | None = None,
    ) -> ToolResult:
        """Dispatch one admitted call as the loop's actor (RM14); a local failure escalates."""
        actor = loop.actor
        result = await self._run_admitted(admitted, proposal_id, actor, mechanism=mechanism)
        if actor == LOCAL_ACTOR and result.status is not ToolStatus.SUCCEEDED:
            raise Escalation(
                "tool_failure",
                f"tool {result.tool_name!r} ended {result.status.value}",
                operation_id=result.operation_id,
                tool_status=result.status.value,
            )
        return result

    async def _execute_set(self, loop: _Loop, turn: _Turn) -> None:
        """Run an admitted set in proposal order, one executor call each (section 4.3)."""
        contents = []
        for admitted, proposal_id in zip(turn.admitted, turn.proposal_ids, strict=True):
            result = await self._dispatch_step(loop, admitted, proposal_id)
            contents.append(result.to_json_object())
        loop.conversation += _tool_exchange_set(turn.calls, contents)

    def _tool_turn(
        self,
        generation: _Generation,
        available: frozenset[str],
        skill: Skill | None,
        *,
        many: bool | None = None,
    ) -> _Turn:
        """Admit a tool turn and record every proposed call (RM05, RM09, RM11, RM16)."""
        calls = tuple(generation.calls)
        if many is None:
            many = self.o.performance.calls_per_generation == "many"
        as_set = many and len(calls) > 1
        offending: int | None = None
        if as_set:
            admitted, rejection, offending = self._admit_set(calls, available, self.results)
        else:
            one, rejection = self._admit(calls, available, self.results)
            admitted = [] if one is None else [one]
        turn = _Turn(calls, tuple(admitted), rejection, (), answer_all=as_set, offending=offending)
        proposal_ids = tuple(
            self._propose(call, skill, call_rejection)
            for call, call_rejection in zip(calls, turn.call_rejections(), strict=True)
        )
        return replace(turn, proposal_ids=proposal_ids)

    async def _run_admitted(
        self, call: ValidatedCall, proposal_id: str, actor: str, *, mechanism: str | None = None
    ) -> ToolResult:
        """Dispatch an admitted call and keep its result as run evidence."""
        result = await self._dispatch(call, proposal_id, actor, mechanism=mechanism)
        self.results.append(result)
        return result

    def _admit(
        self,
        calls: Sequence[ToolCall],
        available: frozenset[str],
        results: Sequence[ToolResult],
    ) -> tuple[ValidatedCall | None, dict[str, str] | None]:
        """The validated call, or the rejection (RM05, RM09, RM11)."""
        if len(calls) != 1:
            return None, {
                "code": "multiple_tool_calls",
                "message": "propose one tool call at a time",
            }
        return self._admit_one(calls[0], available, results)

    def _admit_set(
        self,
        calls: Sequence[ToolCall],
        available: frozenset[str],
        results: Sequence[ToolResult],
    ) -> tuple[list[ValidatedCall], dict[str, str] | None, int | None]:
        """The validated calls of a set, or one rejection of the whole generation (section 4.3)."""
        limit = self.meter.admitted_calls_limit()
        if len(calls) > limit:
            return (
                [],
                {
                    "code": "multiple_tool_calls",
                    "message": f"propose at most {_count(limit, 'tool call')} at a time",
                },
                None,
            )
        if any(self._mutates(call.tool_name) for call in calls):
            return (
                [],
                {
                    "code": "multiple_tool_calls",
                    "message": "a mutating tool call must be the only call of its generation",
                },
                None,
            )
        admitted = []
        for index, call in enumerate(calls):
            one, rejection = self._admit_one(call, available, results)
            if rejection is not None:
                return [], rejection, index
            assert one is not None
            admitted.append(one)
        room = self.meter.tool_room()
        if self._new_executions(admitted) > room:
            return (
                [],
                {
                    "code": "multiple_tool_calls",
                    "message": (
                        f"only {_count(room, 'more tool execution')}"
                        f" {'is' if room == 1 else 'are'} allowed in this run"
                    ),
                },
                None,
            )
        return admitted, None, None

    def _new_executions(self, calls: Iterable[ValidatedCall]) -> int:
        """How many distinct operations the calls would execute; a repeat is a replay (RM10)."""
        return len(
            {
                ToolExecutor.operation_id(self.run_id, call.tool_name, call.arguments)
                for call in calls
                if not self.o.executor.has_operation(call)
            }
        )

    def _mutates(self, tool_name: str) -> bool:
        """Whether the name is a registered tool with side effects."""
        try:
            return self.o.tools.get(tool_name).side_effect is SideEffect.MUTATING
        except KeyError:
            return False

    def _admit_one(
        self,
        call: ToolCall,
        available: frozenset[str],
        results: Sequence[ToolResult],
    ) -> tuple[ValidatedCall | None, dict[str, str] | None]:
        """One call's own checks: RM09 validation, RM05 provenance, a confirmable target."""
        try:
            admitted = self.o.executor.validate(
                self.run_id, call.tool_name, call.arguments, available=available
            )
        except ToolRejectedError as exc:
            return None, {"code": exc.code, "message": str(exc)}
        provenance = identifiers.provenance_text(self.messages, results)
        unsupported = identifiers.unsupported_identifiers(
            admitted.arguments, admitted.tool.declaration.input_schema, provenance
        )
        if unsupported:
            return None, {
                "code": "unsupported_identifier",
                "message": (
                    f"{', '.join(unsupported)} does not come from the request or tool results"
                ),
            }
        if (
            admitted.tool.side_effect is SideEffect.MUTATING
            and admitted.tool.confirmation_target is None
        ):
            return None, {
                "code": CONFIRMATION_UNSUPPORTED,
                "message": (
                    f"tool {call.tool_name!r} is mutating and declares no confirmation target, "
                    "so it cannot be confirmed"
                ),
            }
        return admitted, None

    def _propose(
        self,
        call: ToolCall,
        skill: Skill | None,
        rejection: dict[str, str] | None,
        *,
        mechanism: str | None = None,
    ) -> str:
        """Record a proposed call."""
        proposal_id = f"proposal_{uuid.uuid4().hex[:16]}"
        arguments = self.redact_arguments(call.arguments)
        by_gateway = mechanism is not None
        self.emit(
            EventType.TOOL_PROPOSED,
            GATEWAY_ACTOR if by_gateway else self.actor,
            {
                **self._branch_label(),
                "proposal_id": proposal_id,
                "call_id": call.call_id,
                "tool_name": call.tool_name,
                "arguments": arguments,
                "skill_id": None if skill is None else skill.name,
                "skill_version": None if skill is None else skill.version,
                "run_state_version": self.record.version,
                "model": None if by_gateway else self.active_model.model_id,
                **({"mechanism": mechanism} if by_gateway else {}),
                "admissible": rejection is None,
                "rejection": rejection,
            },
        )
        if by_gateway:
            return proposal_id
        if self.authority.holder == LOCAL_ACTOR:
            self.local_proposals[proposal_id] = {
                "kind": "tool_call",
                "label": "proposal",
                "proposal_id": proposal_id,
                "tool_name": call.tool_name,
                "arguments": arguments,
                "rejection_code": None if rejection is None else rejection["code"],
            }
        return proposal_id

    def _branch_label(self) -> dict[str, str]:
        """The workflow branch the tool events of the moment belong to (spec 0.25)."""
        return {} if self.tool_branch is None else {"branch": self.tool_branch}

    async def _dispatch(
        self, call: ValidatedCall, proposal_id: str, actor: str, *, mechanism: str | None = None
    ) -> ToolResult:
        """Execute (or replay) the call, with the one retry section 11 allows."""
        self.authority.require(actor)
        execution = await self._execute(call, proposal_id, attempt=1, mechanism=mechanism)
        if self.o.executor.may_retry(call, execution.result, attempt=1):
            execution = await self._execute(call, proposal_id, attempt=2, mechanism=mechanism)
        return execution.result

    async def _execute(
        self,
        call: ValidatedCall,
        proposal_id: str,
        *,
        attempt: int,
        mechanism: str | None = None,
    ) -> Execution:
        common = {
            "proposal_id": proposal_id,
            "tool_name": call.tool_name,
            "attempt": attempt,
            **self._branch_label(),
        }
        approval_id = self._approvals.get(proposal_id)
        if approval_id is not None:
            common["approval_id"] = approval_id
        model_id = None if mechanism is not None else self.active_model.model_id
        if mechanism is not None:
            common["mechanism"] = mechanism

        def before_run(operation_id: str) -> None:
            self.meter.charge_tool_call()
            self.emit(
                EventType.TOOL_STARTED,
                GATEWAY_ACTOR,
                {**common, "operation_id": operation_id, "model": model_id},
            )

        def on_finished(execution: Execution) -> None:
            self.secrets |= execution.secrets
            self.local_proposals.pop(proposal_id, None)
            result = execution.result
            self.emit(
                EventType.TOOL_FINISHED,
                GATEWAY_ACTOR,
                {
                    **common,
                    "operation_id": result.operation_id,
                    "model": model_id,
                    "status": result.status.value,
                    "replayed": execution.replayed,
                    "result": result.to_json_object(),
                },
            )

        try:
            execution = await self.o.executor.execute(
                call,
                proposal_id=proposal_id,
                model=GATEWAY_OPERATION_MODEL if model_id is None else model_id,
                attempt=attempt,
                before_run=before_run,
                time_limit=self.meter.remaining_seconds,
                on_finished=on_finished,
                secrets=frozenset(self.secrets),
            )
        except OperationInFlightError as exc:
            raise Escalation("tool_failure", str(exc), operation_id=exc.operation_id) from exc
        if execution.time_limit_exhausted:
            raise self.meter.time_exhausted()
        self.meter.check_time()
        return execution
