"""Bounded local generation loop (RM01-RM05, RM12, RM16, sections 4.1, 7.3, 11).

One run, start to end, with the local model only:

1. accepted: record the section 8 run.accepted payload: the requested
   and effective execution mode, the model alias, the budgets (RM03),
   the skill roots, and the skills offered (section 4.2). That snapshot
   is the one RM07 pins: the selected skill is loaded at exactly that
   version and hash.
2. routing: in external_only the gateway records its own decision
   (route external, reason execution_mode, section 7.4) and runs no
   generation. Otherwise check the adapter's capabilities, measure the
   router input with the local tokenizer, and either take the bypass
   decision for an oversized input (section 4.1) or run the single
   routing generation (RM02, ``relaymind.orchestrator.router``).
   From spec 0.31 the choice goes through the selector chain (RM28,
   ``relaymind.selection``): the routing generation is the model_router
   selector, the default chain, and the gateway admits what a selector
   proposes. A chain that chooses nothing routes external with reason
   selection_failed; a rejected routing output still escalates with
   invalid_output when no other selector chose.
   A non-local route is an escalation (step 5). The run keeps the language
   of this first RouteDecision across every resume (section 5).
2a. Clarify suspends the run in waiting_input with clarification.required
   (RM05; spec 0.6 sections 5 and 8): question, language, reason_code,
   skill_id, the missing input, and waiting_seconds. The stage ends and
   the wait is not active time (section 11). ``LocalOrchestrator.resume``
   takes explicit new input text: it records clarification.answered
   (input redacted, missing, waited_ms), appends the question as an
   assistant turn and the input as a user turn, and opens a new local
   stage with the full per-stage budgets, bounded by the entire-run
   limits. When the clarify decision named a skill, the gateway checks
   the new input for the missing identifier without a routing
   generation; when its skill_id was null, the run routes again with a
   routing generation. At most ``clarification_rounds`` clarifications
   are asked; a further one fails the run with missing_input. A wait
   longer than ``waiting_seconds`` fails with input_expired.
3. local_running: load the selected skill (skill.loaded, RM06) and loop.
   Each generation returns a tool proposal or a finalize proposal (an
   answer text). A tool proposal is validated once (RM09) and checked
   for identifiers that no user turn or tool result named (RM05) before
   the executor runs it (RM10). A mutating tool is not executed on its
   proposal: the run suspends in waiting_confirmation (3a). A failed or
   unknown tool outcome stops the run with reason code tool_failure,
   after the one read-only retry section 11 allows for a transient
   failure. A finalize proposal must
   meet the skill's completion predicate; then a final generation runs
   with tools disabled (section 7.3), its answer is checked by the same
   predicate, and published as answer.delta events. Buffered uses one
   batch; effective live verifies stable statements before each publication
   and checks completeness at normal EOF (RM32). Under answer_pass draft
   (RM31, spec 0.32) the draft that met
   the predicate is published as the answer without that second
   generation, when it is in the run's language, holds no tool-call
   JSON, and the caller sent no answer option; run.completed records
   answer_pass_applied, the pass that produced the answer.
3a. Confirmation (RM11; spec 0.10 sections 4.3 and 5). A mutating tool
   that passed the checks above suspends the run in waiting_confirmation,
   from local_running or from external_running. The gateway issues a
   random single-use token, keeps only its hash with the approval, binds
   the run, tool, canonical arguments, target, and state version, and
   records confirmation.required; the plaintext token goes only to the
   caller in ``RunOutcome.confirmation``. ``resume_confirmation`` takes
   the approval ID, the token, and approve: a mismatch is
   ApprovalRejectedError (403) and the run keeps waiting; an approval
   older than five minutes fails the run with approval_expired; a
   denial cancels it with user_denied and mutates nothing; an approval
   consumes the token, opens a new stage like a clarification resume,
   executes the held call once, and continues the loop. A call that
   already has a recorded operation asks for nothing (RM10).
4. An invalid proposal gets at most the configured format-repair
   generations, inside the generation limit (RM03).
4a. Admitted calls per generation (spec 0.34 section 4.3, AC18). Under
   calls_per_generation many, a generation's read-only calls are judged
   as a set: more than the limit, a mutating call among several, one
   invalid call, or a set with more new executions than the tool budget has
   room for rejects the whole generation before anything runs, and the
   rejection answers every call with what its tool.proposed records: the
   failed call with its own rejection and the others as not executed, or,
   for a rejection about the set, every call with it. The room counts one execution per call, so
   a retry of a transient read failure (section 11) can still use the last
   of it in the middle of a set; that ends the run on budget like a single
   call would.
   An admitted set runs in proposal order, each call as its own operation.
   The parallel workflow's local branch keeps the one-call rule (RM15).
4b. Prefetch (RM29, spec 0.34, AC18). Under prefetch true, the calls a skill declares
   run before its first generation in a local stage, or in an external stage
   that loaded the skill without a local stage before it: the gateway resolves
   each from the request by the RM05 rule, runs them together through the
   same checks as a proposal (actor gateway, mechanism prefetch, model
   null), charges them to the budgets, and adds the exchange to the
   conversation. Dispatch authority does not move (RM14).

Every generation is charged by its purpose in one place, ``_generate``,
before it runs, and every tool execution is charged when the executor
is about to run it (a replay is not an execution). Exceeding a budget
is an explicit stop with reason code local_budget, never silent
continuation (RM04). A tool is bounded by its timeout or the remaining
active budget, whichever is shorter.

5. Escalation (RM04, RM13, RM14). Every stop above is an Escalation with
   an RM04 reason code. If external dispatch is permitted (an external
   model is configured, the mode is not local_only, and the entire-run
   limits leave room), ``_escalate`` builds the HandoffEnvelope from the
   run store (``relaymind.orchestrator.handoff``), measures the external
   request against the external context limit, records handoff.started,
   and moves the run to external_running. The local actor's dispatch
   authority is revoked at that point; the external model then proposes
   tools that run through the same validation, provenance check, and
   executor, and it owns the final answer (RM14, RM20). A proposal that
   repeats a completed operation gets the recorded result and runs no
   handler (RM10). Evidence over the external context limit fails the
   run with context_limit_exceeded before any external call (RM13); an
   external stage that cannot finish fails with external_failed
   (section 11), with the escalation's reason code. If dispatch is not
   permitted, the run ends as failed with error code needs_external and
   the RM04 reason code; partial evidence stays in the trace (section 5).
   Some stops never hand off (section 4.4): a stop of the parallel
   workflow (RM15, 5a); an unavailable
   local model unless the operator policy permits degraded mode (section
   11), which records route.selected with mechanism degraded_mode when the
   stage has no routing decision yet, and degraded_mode and local_failure in
   handoff.started; and a selected skill with no permitted tool (RM08).

5a. The parallel workflow (RM14, RM15, RM16; spec 0.25 section 4.4). A run
   that routes parallel loads the skill and moves to parallel_running. The
   local_health branch asks the local model for the skill's read-only
   tool call and executes it; the external_checklist branch asks the
   external model for a provisional checklist from the request alone.
   The branches run as two tasks and never see each other. When both
   finish, the gateway records handoff.started, moves to external_running,
   revokes the local actor's dispatch authority, and makes a new external
   call (the synthesis) that combines the tool result and the draft; that
   answer must meet the skill's completion predicate and is published by
   the external model. A failed branch cancels the other; a cancel stops
   both. Every branch call is in the model_calls inventory, abandoned
   ones included, and the terminal event carries the workflow summary.

Without a usable local model (RM26, spec 0.31 sections 7.4 and 11). The
orchestrator may have no local model (absent), and holds the class of
the one it has (``LocalStatus``: ready, unavailable, unsuitable), which
startup detection sets and readiness updates. Each run records the class
it was accepted under in run.accepted and keeps it. In auto, a class
other than ready is managed external execution: the selector chain runs
without model_router, the gateway admits the choice with its own rules
(the RM05 clarification, otherwise route external with the skill and
reason no_local_model), route.selected records local_model_status, and
the run continues through step 5 with the skill loaded. This is never
degraded mode. local_only and parallel_demo are refused while the class
does not permit them (``ExecutionModeForbiddenError``, 403).

Native runs (RM21, spec 0.17 section 7.4). ``start``, ``start_resume``,
and ``start_confirmation`` do the synchronous part of ``run``,
``resume``, and ``resume_confirmation`` (every check, the slot, the
transition) and continue the run in a background task, so the HTTP
request that asked is not the run's owner. ``cancel`` ends a waiting
run at once and asks a background run to stop; a run that a caller
awaits directly is not cancelled this way. ``reveal_confirmation``
hands out a confirmation token once.

Any other exception, in the first stage or after a resume, fails the
run with error code internal_error and its run.failed event, which
releases the execution slot, and then propagates; a cancelled run ends
as cancelled. A terminal run never changes again (RM17).

The trace proves locality (RM01, RM16): every model call is labelled
with the locality its adapter declares, and run.completed lists every
model call and the number of them that were external. run.completed and
run.failed report active_ms and waiting_ms separately (RM22, section
12.1); waiting_ms is the sum of the recorded waits.

Tool arguments, tool results, and model text are redacted of configured
secret fields, and of the secret values the run has seen, before they
are persisted (RM12, section 8).
"""

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from typing import Any, TypeVar

from relaymind.adapters import (
    AdapterCapabilities,
    GenerationOptions,
    Message,
    ModelAdapter,
    Role,
    TokenCountError,
    UnsupportedCapabilityError,
)
from relaymind.orchestrator._run_types import (
    _RESUME_MISMATCH,
    ANSWER_LANGUAGE,
    BRANCH_CORRECTION,
    DRAFT_HEADER,
    DRAFT_INSTRUCTION,
    EXTERNAL_CHECKLIST,
    LOCAL_HEALTH,
    PARALLEL_WORKFLOW,
    PROVIDER_CANCEL_SECONDS,
    SELECTION_TIMEOUT_BUDGET,
    SYNTHESIS,
    SYNTHESIS_INSTRUCTION,
    WORKFLOW_UNAVAILABLE,
    _Confirmation,
    _ConfirmationNeeded,
    _correction_exchange,
    _external_correction,
    _finish,
    _LivePredicateFailure,
    _Loop,
    _Pending,
    _Step,
    _Workflow,
)
from relaymind.orchestrator._run_types import (
    APPROVAL_EXPIRED as APPROVAL_EXPIRED,
)
from relaymind.orchestrator._run_types import (
    APPROVAL_INVALID as APPROVAL_INVALID,
)
from relaymind.orchestrator._run_types import (
    BODY_HEADER as BODY_HEADER,
)
from relaymind.orchestrator._run_types import (
    CONFIRMATION_UNSUPPORTED as CONFIRMATION_UNSUPPORTED,
)
from relaymind.orchestrator._run_types import (
    CONTEXT_LIMIT_EXCEEDED as CONTEXT_LIMIT_EXCEEDED,
)
from relaymind.orchestrator._run_types import (
    CORRECTION as CORRECTION,
)
from relaymind.orchestrator._run_types import (
    CRITERIA_VALUES_HEADER as CRITERIA_VALUES_HEADER,
)
from relaymind.orchestrator._run_types import (
    EMPTY_ANSWER_CHECK as EMPTY_ANSWER_CHECK,
)
from relaymind.orchestrator._run_types import (
    EXTERNAL_CORRECTION as EXTERNAL_CORRECTION,
)
from relaymind.orchestrator._run_types import (
    EXTERNAL_FAILED as EXTERNAL_FAILED,
)
from relaymind.orchestrator._run_types import (
    EXTERNAL_OPTIONS as EXTERNAL_OPTIONS,
)
from relaymind.orchestrator._run_types import (
    FINAL_INSTRUCTION as FINAL_INSTRUCTION,
)
from relaymind.orchestrator._run_types import (
    GATEWAY_ACTOR as GATEWAY_ACTOR,
)
from relaymind.orchestrator._run_types import (
    INPUT_EXPIRED as INPUT_EXPIRED,
)
from relaymind.orchestrator._run_types import (
    INTERNAL_ERROR as INTERNAL_ERROR,
)
from relaymind.orchestrator._run_types import (
    INVALID_RESUME_INPUT as INVALID_RESUME_INPUT,
)
from relaymind.orchestrator._run_types import (
    LOCAL_OPTIONS as LOCAL_OPTIONS,
)
from relaymind.orchestrator._run_types import (
    LOCALITIES as LOCALITIES,
)
from relaymind.orchestrator._run_types import (
    MISSING_INPUT as MISSING_INPUT,
)
from relaymind.orchestrator._run_types import (
    NEEDS_EXTERNAL as NEEDS_EXTERNAL,
)
from relaymind.orchestrator._run_types import (
    NOT_EXECUTED as NOT_EXECUTED,
)
from relaymind.orchestrator._run_types import (
    PREFETCH as PREFETCH,
)
from relaymind.orchestrator._run_types import (
    RESUME_MECHANISM as RESUME_MECHANISM,
)
from relaymind.orchestrator._run_types import (
    ROUTING_MECHANISM as ROUTING_MECHANISM,
)
from relaymind.orchestrator._run_types import (
    SKILL_INSTRUCTION as SKILL_INSTRUCTION,
)
from relaymind.orchestrator._run_types import (
    USER_DENIED as USER_DENIED,
)
from relaymind.orchestrator._run_types import (
    ApprovalRejectedError as ApprovalRejectedError,
)
from relaymind.orchestrator._run_types import (
    ConfiguredModel as ConfiguredModel,
)
from relaymind.orchestrator._run_types import (
    ConfirmationRequest as ConfirmationRequest,
)
from relaymind.orchestrator._run_types import (
    ContextLimitExceeded as ContextLimitExceeded,
)
from relaymind.orchestrator._run_types import (
    Escalation as Escalation,
)
from relaymind.orchestrator._run_types import (
    ExecutionModeForbiddenError as ExecutionModeForbiddenError,
)
from relaymind.orchestrator._run_types import (
    ExternalFailure as ExternalFailure,
)
from relaymind.orchestrator._run_types import (
    ResumeInputError as ResumeInputError,
)
from relaymind.orchestrator._run_types import (
    RunOutcome as RunOutcome,
)
from relaymind.orchestrator._run_types import (
    StartedRun as StartedRun,
)
from relaymind.orchestrator._run_types import (
    _arguments_digest as _arguments_digest,
)
from relaymind.orchestrator._run_types import (
    _count as _count,
)
from relaymind.orchestrator._run_types import (
    _digest as _digest,
)
from relaymind.orchestrator._run_types import (
    _Generation as _Generation,
)
from relaymind.orchestrator._run_types import (
    _PublicationFailure as _PublicationFailure,
)
from relaymind.orchestrator._run_types import (
    _tool_exchange as _tool_exchange,
)
from relaymind.orchestrator._run_types import (
    _tool_exchange_set as _tool_exchange_set,
)
from relaymind.orchestrator._run_types import (
    _Turn as _Turn,
)
from relaymind.orchestrator._run_types import (
    not_executed_rejection as not_executed_rejection,
)
from relaymind.orchestrator._run_types import (
    skill_stage_messages as skill_stage_messages,
)
from relaymind.orchestrator.budgets import BudgetExceededError, BudgetMeter, LocalBudgets
from relaymind.orchestrator.confirmation import _ConfirmationMixin
from relaymind.orchestrator.generation import _GenerationMixin
from relaymind.orchestrator.handoff import (
    EXTERNAL_ACTOR,
    LOCAL_ACTOR,
    DispatchAuthority,
    HandoffEnvelope,
    RequestSize,
    build_handoff_envelope,
    external_messages,
    measure_request,
    unsettled_operations,
)
from relaymind.orchestrator.local_status import (
    STAGE_STREAK,
    LocalModelClass,
    LocalStatus,
    stage_status,
)
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.placement import Placement, PlacementSettings, marker
from relaymind.orchestrator.prefetch import check_prefetch_budgets
from relaymind.orchestrator.publish import _PublishMixin
from relaymind.orchestrator.recovery import (
    RecoveryReport,
    model_calls,
    recover_interrupted_runs,
)
from relaymind.orchestrator.route_decision import ExecutionMode, Language, Route, RouteDecision
from relaymind.orchestrator.router import Router, Routing, request_language
from relaymind.orchestrator.routing_cache import RoutingCache
from relaymind.orchestrator.run_state import (
    CANCEL_REQUESTED,
    TERMINAL_STATES,
    WAITING_STATES,
    InvalidTransitionError,
    RunState,
    RunStateMachine,
    SlotUnavailableError,
    TerminalStateError,
)
from relaymind.orchestrator.selection import _SelectionMixin
from relaymind.orchestrator.tools import _ToolsMixin
from relaymind.redaction import Redactor, redact_text
from relaymind.selection import (
    EXAMPLE_MATCH,
    MODEL_ROUTER,
    ExampleMatchSelector,
    ModelRouterSelector,
    SelectionSettings,
    Selector,
    SelectorChain,
)
from relaymind.selection.http import HttpSelector
from relaymind.skills import Skill, SkillRegistry
from relaymind.skills.predicates import (
    COMPLETION_CRITERIA,
    COMPLETION_PREDICATES,
    pending_evidence_complete,
)
from relaymind.store import (
    IdempotencyClaim,
    RunRecord,
    RunStore,
    StaleVersionError,
    TraceClock,
)
from relaymind.store.trace_event import EventType
from relaymind.tools import CallerContext, OperatorPolicy, ToolRegistry
from relaymind.tools.executor import ToolExecutor
from relaymind.tools.registry import ToolResult

_T = TypeVar("_T")


class AcceptedRun:
    """A run that holds the execution slot, with run.accepted written, and no work started."""

    def __init__(
        self,
        owner: "LocalOrchestrator",
        run: "_Run",
        work: Callable[[], Awaitable[RunOutcome]],
    ) -> None:
        self._owner = owner
        self._run = run
        self._work = work

    @property
    def run_id(self) -> str:
        return self._run.run_id

    def start(self, convert: Callable[[RunOutcome], _T]) -> "asyncio.Task[_T]":
        async def drive() -> _T:
            return convert(self._owner._keep_if_waiting(self._run, await self._work()))

        task = asyncio.get_running_loop().create_task(drive())
        self._owner._owned[task] = self._run
        task.add_done_callback(self._settle)
        return task

    def _settle(self, task: "asyncio.Task[Any]") -> None:
        self._owner._owned.pop(task, None)
        if task.cancelled():
            self._run._abort(asyncio.CancelledError())
        else:
            task.exception()


# The attribute that marks an exception raised by a run's own work (section 11).
_ABORTED_RUN = "_relaymind_aborted_run"


def aborted_run_id(exc: BaseException) -> str | None:
    """The run whose own work raised exc, or None for any other exception."""
    run_id = getattr(exc, _ABORTED_RUN, None)
    return run_id if isinstance(run_id, str) else None


class LocalOrchestrator:
    """Runs managed requests through the bounded local loop."""

    def __init__(
        self,
        model: ConfiguredModel | None,
        *,
        skills: SkillRegistry,
        tools: ToolRegistry,
        store: RunStore,
        policy: OperatorPolicy,
        caller: CallerContext,
        budgets: LocalBudgets | None = None,
        clock: TraceClock | None = None,
        redactor: Redactor | None = None,
        enabled_workflows: Collection[str] = (),
        parallel_in_auto: bool = False,
        external: ConfiguredModel | None = None,
        degraded_mode: bool = False,
        routing_cache: RoutingCache | None = None,
        performance: PerformanceSettings | None = None,
        selection: SelectionSettings | None = None,
        selectors: Sequence[Selector] | None = None,
        local_status: LocalStatus | None = None,
        detect: bool = False,
        detect_stages: bool = False,
        placement: PlacementSettings | None = None,
    ) -> None:
        """selection is the [selection] table (spec 0.31 section 14.1); its default is the
        baseline chain, model_router alone. selectors replaces the configured chain with
        selector objects, for tests; the launcher never passes it.

        model is None when no local model is configured (RM26: absent). local_status
        is the class of a configured one, ready by default; startup detection sets it
        later with ``set_local_status``. detect lets readiness repeat the availability
        check and change the class (placement.detect, section 14.1); off by default, so
        an unavailable model is only reported not ready, as before spec 0.31.
        detect_stages (placement.detect_stages, spec 0.48) adds the skill-shaped
        warm-up to the startup check and the local stage rule of section 14.4.7;
        off by default, the spec 0.47 behavior.
        """
        self.model = model
        self._local_status = self._checked_status(local_status)
        self.local_startup_pending = False
        # The last measured unsuitable class: a model that comes back from unavailable
        # is unsuitable again until a measurement says otherwise (RM26).
        self._unsuitable: LocalStatus | None = (
            self._local_status if self._local_status.status == LocalModelClass.UNSUITABLE else None
        )
        self.detect = detect
        self.detect_stages = detect_stages
        # Spec 0.32 section 14.1: every default is the baseline behavior.
        self.performance = performance or PerformanceSettings()
        if self.performance.prefetch:
            # RM29: a skill whose prefetch calls alone exceed a limit is a startup error.
            check_prefetch_budgets(skills, budgets or LocalBudgets())
        # Spec 0.26 section 10: None (the default) runs without a routing cache.
        self.routing_cache = routing_cache
        self.external = external
        # Operator policy (section 11): an unavailable local model may
        # route directly to external. Off by default.
        self.degraded_mode = degraded_mode
        self.skills = skills
        self.tools = tools
        self.enabled_workflows = frozenset(enabled_workflows)
        self.placement = Placement(store, placement)
        self.store = store
        self.policy = policy
        self.caller = caller
        self.budgets = budgets or LocalBudgets()
        # One TraceClock for budgets, durations, and the store (RM25).
        self.clock = clock or store.clock
        self.redactor = redactor or Redactor()
        self.router = Router(
            skills,
            tools,
            budgets=self.budgets,
            enabled_workflows=enabled_workflows,
            parallel_in_auto=parallel_in_auto,
        )
        # RM28: the selector chain, in configured order.
        self.selection = selection or SelectionSettings()
        built_in: dict[str, Callable[[], Selector]] = {
            MODEL_ROUTER: lambda: ModelRouterSelector(self.router),
            EXAMPLE_MATCH: lambda: ExampleMatchSelector(
                tools,
                score=self.selection.example_match_score,
                margin=self.selection.example_match_margin,
            ),
        }
        chain = (
            selectors
            if selectors is not None
            else [
                built_in[n]()
                if isinstance(n, str)
                else HttpSelector(name=n.name, version=n.version, url=n.url)
                for n in self.selection.chain
            ]
        )
        self.selection_chain = SelectorChain(
            chain, timeout_seconds=self.selection.timeout_seconds, clock=self.clock.monotonic
        )
        # RM26: without a usable local model the chain runs without a model call, so
        # without model_router. A chain left empty uses the default chain of a gateway
        # without a local model, example_match alone (section 14.1).
        no_model = [s for s in chain if not s.model_based] or [built_in[EXAMPLE_MATCH]()]
        self.no_local_chain = SelectorChain(
            no_model, timeout_seconds=self.selection.timeout_seconds, clock=self.clock.monotonic
        )
        self.executor = ToolExecutor(tools, store, clock=self.clock, redactor=self.redactor)
        self.machine = RunStateMachine(store)
        # Runs this orchestrator suspended in waiting_input or
        # waiting_confirmation. A process restart fails unfinished runs as
        # interrupted (section 5), so nothing here has to outlive the process.
        self._waiting: dict[str, _Run] = {}
        # Runs this orchestrator drives in a background task of their own
        # (native runs), for cancellation; and the confirmation requests
        # whose token has not been read yet (RM11, spec 0.17).
        self._driven: dict[str, tuple[_Run, asyncio.Task[RunOutcome]]] = {}
        self._issued: dict[str, ConfirmationRequest] = {}
        # Runs a request owns, by the task that awaits them, so a chat
        # disconnect can cancel one as the native cancel does (section 7.4).
        self._owned: dict[asyncio.Task[Any], _Run] = {}
        # Requests to adapters to cancel a generation, still in progress;
        # held so a pending one is not garbage collected (section 7.4).
        self._provider_cancels: set[asyncio.Task[None]] = set()
        # Set once this orchestrator accepts a run; restart recovery is
        # refused from then on (section 5).
        self._accepted_a_run = False

    # -- the class of the local model (RM26) ----------------------------------------

    def _checked_status(self, status: LocalStatus | None) -> LocalStatus:
        """A class that agrees with the configuration: absent exactly without a model."""
        if self.model is None:
            if status is not None and status.status != LocalModelClass.ABSENT:
                raise ValueError(f"no local model is configured, so it cannot be {status.status}")
            return status or LocalStatus.absent()
        if status is not None and status.status == LocalModelClass.ABSENT:
            raise ValueError("a configured local model cannot be absent")
        return status or LocalStatus.ready()

    @property
    def local_status(self) -> LocalStatus:
        """The class of the local model now; each run keeps the one it was accepted under.

        With detection and placement.detect_stages on, a ready model whose latest
        local stages kept running out of local time is unsuitable (RM26, spec 0.48
        section 14.4.7).
        """
        if self.local_startup_pending:
            return LocalStatus(LocalModelClass.UNAVAILABLE, "the startup model check is pending")
        if (
            not (self.detect and self.detect_stages)
            or self.model is None
            or not self._local_status.usable
        ):
            return self._local_status
        measured = stage_status(
            self.store.latest_local_stage_outcomes(self.model.model_id, limit=STAGE_STREAK),
            now=self.store.clock.utc_now(),
            probe_after_seconds=self.placement.settings.probe_after_seconds,
            active_seconds=self.budgets.active_seconds,
        )
        return measured or self._local_status

    def set_local_status(self, status: LocalStatus) -> None:
        """Set the class startup detection measured (RM26)."""
        self._local_status = self._checked_status(status)
        if status.status == LocalModelClass.UNSUITABLE:
            self._unsuitable = status
        elif status.status == LocalModelClass.READY:
            self._unsuitable = None

    def observe_local_health(self, available: bool, warm: bool, detail: str | None) -> None:
        """Readiness repeated the availability check (RM26, section 7.4).

        Only with detection on. A model that stops answering or is no longer
        loaded is unavailable; one that answers again with the model loaded
        is used again, and a model last measured unsuitable is unsuitable again.
        """
        if not self.detect or self.model is None or self.local_startup_pending:
            return
        current = self._local_status.status
        if current == LocalModelClass.UNAVAILABLE and available and warm:
            self._local_status = self._unsuitable or LocalStatus.ready()
        elif current != LocalModelClass.UNAVAILABLE and not (available and warm):
            cause = detail.strip() if detail and detail.strip() else None
            reason = (
                f"the model is not loaded: {cause or 'the model is loading'}"
                if available
                else f"the runtime stopped answering: {cause or 'no answer'}"
            )
            self._local_status = LocalStatus(LocalModelClass.UNAVAILABLE, reason)

    def mode_refusal(self, mode: ExecutionMode) -> str | None:
        """Why the class of the local model does not permit mode, or None (section 7.4)."""
        return self.local_status.refusal(mode)

    async def run_text(self, text: str, **options: Any) -> RunOutcome:
        """Run one user request given as plain text."""
        return await self.run((Message(role=Role.USER, content=text),), **options)

    async def run(
        self,
        messages: Sequence[Message],
        *,
        execution_mode: str | None = None,
        model_alias: str | None = None,
        final_options: GenerationOptions | None = None,
        idempotency: IdempotencyClaim | None = None,
        answer_stream_effective: str | None = None,
    ) -> RunOutcome:
        """Run one managed request to a terminal or waiting state.

        execution_mode is the requested mode (None means auto, section
        7.4); operator policy checks belong to the API layer. model_alias
        is the chat request's virtual alias, or None for a native run.
        final_options are the caller's token budget and sampling for the
        generations that write the answer: the local final generation and
        the external generations (spec 0.14 section 7.1). An external
        generation offered tools may propose a tool call, so it takes the
        sampling but keeps its own token budget. Routing, skill, and
        repair generations keep their controlled settings (RM03).
        idempotency is the caller's key, stored with the run when it is
        created (spec 0.18 section 7.4); the API layer answers a stored
        key before it gets here.
        Raises SlotUnavailableError (429) when another run holds the
        execution slot; no run is created then.
        """
        run = _Run(
            self,
            tuple(messages),
            execution_mode,
            model_alias,
            final_options,
            idempotency,
            answer_stream_effective,
        )
        with self.owning(run):
            return self._keep_if_waiting(run, await run.execute())

    @contextlib.contextmanager
    def owning(self, run: "_Run") -> Iterator[None]:
        """Mark run as owned by the task awaiting it, for request_cancel."""
        task = asyncio.current_task()
        assert task is not None  # only called from a coroutine
        self._owned[task] = run
        try:
            yield
        finally:
            self._owned.pop(task, None)

    def request_cancel(self, task: "asyncio.Task[Any]") -> str | None:
        """Cancel the run a request task owns, as the native cancel does (RM14, AC12).

        A chat disconnect uses this (spec 0.24 section 7.4): dispatch
        stops, the adapter of a generation in flight is asked to cancel
        it, and the task is cancelled so the run ends as cancelled with
        its in-flight tool operations settled. A task that owns no run
        is still cancelled, so its work stops too. Returns the ID of the
        owned run, when it has one.
        """
        run = self._owned.get(task)
        if run is None:
            task.cancel()
            return None
        _stop(run, task)
        record = getattr(run, "record", None)
        return None if record is None else record.run_id

    async def resume(self, run_id: str, expected_version: int, text: object) -> RunOutcome:
        """Resume a run waiting for input with explicit new input (section 5, RM17).

        A terminal run never restarts (TerminalStateError) and a stale
        version is 409 (StaleVersionError). Missing or blank input text is
        400 invalid_resume_input (ResumeInputError) and leaves the run
        waiting with its version unchanged (sections 7.4, 11); the body is
        checked before the waiting limit, so a malformed resume never
        changes the run, even after the wait expired. A valid resume of a
        wait older than waiting_seconds ends the run as failed with
        input_expired. If another run holds the execution slot, the resume
        is 429 and the run keeps waiting (section 5).
        """
        run, step = self._begin_resume(run_id, expected_version, text)
        return self._keep_if_waiting(run, await _finish(step))

    def start_resume(self, run_id: str, expected_version: int, text: object) -> StartedRun:
        """Resume as ``resume`` does, but continue the run in a background task.

        Every check of ``resume`` happens before this returns, with the
        same errors; the continuation then runs on its own (RM21, spec
        0.17 section 7.4). Call it inside a running event loop.
        """
        run, step = self._begin_resume(run_id, expected_version, text)
        return self._start(run, step)

    def _begin_resume(
        self, run_id: str, expected_version: int, text: object
    ) -> "tuple[_Run, _Step]":
        run = self._waiting_run(run_id, expected_version, RunState.WAITING_INPUT)
        if not isinstance(text, str) or not text.strip():
            raise ResumeInputError("a resume in waiting_input needs non-blank input text")
        if run.wait_expired():
            # Untracked only once the expiry is recorded, so a store
            # error leaves the wait for the next sweep (section 5).
            done = False
            try:
                outcome = run.expire()
                done = True
            finally:
                if done or not self._still_waiting(run_id, default=True):
                    self._drop_waiting(run_id)
            return run, outcome
        done = False
        try:
            run.reopen(expected_version)
            done = True
        finally:
            if done or not self._still_waiting(run_id, default=True):
                self._drop_waiting(run_id)
        return run, lambda: run.continue_with(text)

    def recover_after_restart(self) -> RecoveryReport:
        """Fail every unfinished run as interrupted, once, at startup (section 5, RM17).

        Pending writes become unknown_outcome and are never retried;
        pending approvals expire; no model loop resumes. Call it before
        this orchestrator accepts a run. It refuses once this
        orchestrator has accepted one, because a run of this process is
        alive, not interrupted. Calling it again before that is safe.
        """
        if self._accepted_a_run:
            raise RuntimeError("restart recovery is a startup step; this process accepted a run")
        self.store.recover_probe_after_restart()
        return recover_interrupted_runs(self.store, self.machine, self.executor)

    async def resume_confirmation(
        self,
        run_id: str,
        expected_version: int,
        *,
        approval_id: object,
        token: object,
        approve: object,
    ) -> RunOutcome:
        """Answer a pending confirmation with an approval or a denial (RM11, RM17, section 5).

        A terminal run never restarts (TerminalStateError) and a stale
        version is 409 (StaleVersionError). A body of the wrong shape, or
        a run that waits for input, is 400 invalid_resume_input
        (ResumeInputError) and the run keeps waiting. An approval ID or
        token that does not match is ApprovalRejectedError (403); the
        run keeps waiting and nothing is consumed. An approval older than
        five minutes fails the run with approval_expired. If another run
        holds the execution slot, an approval is 429 and keeps the
        approval (section 5). A denial cancels the run with user_denied.
        An approval executes the held call once and continues the run.
        """
        run, step = self._begin_confirmation(
            run_id, expected_version, approval_id=approval_id, token=token, approve=approve
        )
        return self._keep_if_waiting(run, await _finish(step))

    def start_confirmation(
        self,
        run_id: str,
        expected_version: int,
        *,
        approval_id: object,
        token: object,
        approve: object,
    ) -> StartedRun:
        """Answer a confirmation as ``resume_confirmation`` does, continuing in the background.

        Every check, the consumption of the approval, and the transition
        happen before this returns (RM21, spec 0.17 section 7.4). Call it
        inside a running event loop.
        """
        run, step = self._begin_confirmation(
            run_id, expected_version, approval_id=approval_id, token=token, approve=approve
        )
        return self._start(run, step)

    def _begin_confirmation(
        self,
        run_id: str,
        expected_version: int,
        *,
        approval_id: object,
        token: object,
        approve: object,
    ) -> "tuple[_Run, _Step]":
        run = self._waiting_run(run_id, expected_version, RunState.WAITING_CONFIRMATION)
        if not (
            isinstance(approval_id, str)
            and approval_id.strip()
            and isinstance(token, str)
            and token.strip()
            and isinstance(approve, bool)
        ):
            raise ResumeInputError(
                "a resume in waiting_confirmation needs approval_id, token, and a boolean approve"
            )
        try:
            step = run.begin_decision(approval_id, token, approve)
        finally:
            # A store that cannot be read keeps the run tracked, as in
            # expire_waiting, unless begin_decision already cleared the wait.
            if run.confirmation is None or not self._still_waiting(run_id, default=True):
                self._drop_waiting(run_id)
        return run, step

    def start(
        self,
        messages: Sequence[Message],
        *,
        execution_mode: str | None = None,
        model_alias: str | None = None,
        final_options: GenerationOptions | None = None,
        idempotency: IdempotencyClaim | None = None,
        answer_stream_effective: str | None = None,
    ) -> StartedRun:
        """Accept a managed request and run it in a background task (RM21).

        The run exists, in routing, when this returns: the execution slot
        is taken or SlotUnavailableError (429) is raised and nothing is
        created. The work then continues without the caller, so a
        disconnect of whoever asked does not stop it. Call it inside a
        running event loop. idempotency is stored with the run as run()
        stores it.
        """
        run = _Run(
            self,
            tuple(messages),
            execution_mode,
            model_alias,
            final_options,
            idempotency,
            answer_stream_effective,
        )
        run.open()
        return self._start(run, run.drive)

    def accept(
        self,
        messages: Sequence[Message],
        *,
        execution_mode: str | None = None,
        model_alias: str | None = None,
        final_options: GenerationOptions | None = None,
        idempotency: IdempotencyClaim | None = None,
        answer_stream_effective: str | None = None,
    ) -> AcceptedRun:
        """Accept a managed request and return the run before any work starts (RM32).

        The synchronous part of run(): the execution slot is taken or
        SlotUnavailableError (429) is raised and nothing is created, and
        run.accepted is written. The caller drives the work with
        AcceptedRun.start, and its task owns the run (request_cancel).
        """
        run = _Run(
            self,
            tuple(messages),
            execution_mode,
            model_alias,
            final_options,
            idempotency,
            answer_stream_effective,
        )
        run.open()
        return AcceptedRun(self, run, run.drive)

    async def cancel(
        self, run_id: str, expected_version: int, *, wait_seconds: float = 5.0
    ) -> RunRecord:
        """Cancel a run on request and return its record (RM17, RM21, spec 0.17 section 7.4).

        A terminal run never changes (TerminalStateError) and a stale
        version is 409 (StaleVersionError). A waiting run is cancelled
        at once; its pending approval, if any, is voided in the same
        transaction. A run driven by a background task is asked to
        stop, and the call waits up to wait_seconds for it to end, so
        the record can still be active when the work did not stop in
        time (best effort). A run active under a request that owns it is
        InvalidTransitionError: closing that request cancels it.

        A wait past its limit ends as failed (input_expired or
        approval_expired) first, as every other request ends it, so the
        cancel then finds a terminal run (section 5).
        """
        tracked = self._waiting.get(run_id)
        if tracked is not None and self._still_waiting(run_id) and tracked.wait_expired():
            try:
                tracked.expire()
            finally:
                if tracked.waiting_since is None or not self._still_waiting(run_id, default=True):
                    self._drop_waiting(run_id)
        record = self.store.get_run(run_id)
        state = RunState(record.state)
        tracked = self._waiting.get(run_id)
        if state not in WAITING_STATES or (
            tracked is not None and record.version != tracked.record.version
        ):
            self._drop_waiting(run_id)
        if state in TERMINAL_STATES:
            self._driven.pop(run_id, None)
            raise TerminalStateError(f"run {run_id} is {state}; terminal runs never restart")
        if record.version != expected_version:
            raise StaleVersionError(run_id, expected_version, record.version)
        if state in WAITING_STATES:
            run = self._waiting.get(run_id)
            if run is not None:
                try:
                    record = run.cancel_waiting(expected_version)
                finally:
                    if run.waiting_since is None or not self._still_waiting(run_id, default=True):
                        self._drop_waiting(run_id)
            else:
                record = self._cancel_untracked(run_id, expected_version)
                self._drop_waiting(run_id)
            return record
        driven = self._driven.get(run_id)
        if driven is None or driven[1].done():
            raise InvalidTransitionError(
                f"run {run_id} is {state.value} under a request that owns it; "
                "close that request to cancel it"
            )
        run, task = driven
        _stop(run, task)
        await asyncio.wait({task}, timeout=wait_seconds)
        return self.store.get_run(run_id)

    def ask_provider_to_cancel(self, adapter: ModelAdapter, generation_id: str) -> None:
        """Ask an adapter to cancel one generation, without waiting (RM14, AC12).

        Best effort (spec 0.19 section 7.4): the request starts at once, so
        an adapter whose cancel does not wait has acted when this returns.
        The run never waits for it, its failure is never the run's, and it
        is dropped after PROVIDER_CANCEL_SECONDS.
        """

        async def ask() -> None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(PROVIDER_CANCEL_SECONDS):
                    await adapter.cancel(generation_id)

        # The loop is passed explicitly: on CPython 3.12 an eager Task built
        # without it fails before the loop is resolved.
        request = asyncio.Task(ask(), loop=asyncio.get_running_loop(), eager_start=True)
        if not request.done():
            self._provider_cancels.add(request)
            request.add_done_callback(self._provider_cancels.discard)

    def _cancel_untracked(self, run_id: str, expected_version: int) -> RunRecord:
        """Cancel a waiting run this process no longer tracks, from the store alone.

        As for a tracked run, the pending approval is voided in the same
        transaction and run.cancelled names it (RM11, RM17, spec 0.17).
        """
        store = self.store
        calls = model_calls(store, run_id)
        payload: dict[str, Any] = {"reason": CANCEL_REQUESTED, **store.state_timings(run_id)}
        if calls:
            payload["model_calls"] = calls
            payload["external_calls"] = sum(1 for c in calls if c["actor"] == EXTERNAL_ACTOR)
        with store.transaction():
            pending = store.get_pending_approval(run_id)
            if pending is not None:
                payload["approval_id"] = pending["approval_id"]
                store.expire_approvals(run_id)
            return self.machine.cancel(
                run_id,
                expected_version,
                reason=CANCEL_REQUESTED,
                event=(EventType.RUN_CANCELLED, GATEWAY_ACTOR, payload),
            )

    def reveal_confirmation(
        self,
        run_id: str,
        *,
        approval_id: str | None = None,
        verify_store: bool = True,
    ) -> ConfirmationRequest | None:
        """The pending confirmation with its token, the first time it is asked for (RM11).

        Later calls return None, and so does a run with nothing pending:
        the plaintext token is shown once and never stored (spec 0.17
        section 7.4). When approval_id is supplied, a newer issued token
        with a different approval_id is left untouched.
        """
        held = self._issued.get(run_id)
        if held is None or run_id not in self._waiting:
            if run_id not in self._waiting:
                self._issued.pop(run_id, None)
            return None
        if approval_id is not None and held.approval_id != approval_id:
            return None
        if verify_store:
            pending = self.store.get_pending_approval(run_id)
            if pending is None or pending["approval_id"] != held.approval_id:
                self._issued.pop(run_id, None)
                return None
        return self._issued.pop(run_id, None)

    def _start(self, run: "_Run", step: "_Step") -> StartedRun:
        """Drive a prepared step in a background task, or report an ended run."""
        if isinstance(step, RunOutcome):
            self._keep_if_waiting(run, step)
            return self._started(run.run_id)

        async def drive() -> RunOutcome:
            return self._keep_if_waiting(run, await step())

        def settle(task: "asyncio.Task[RunOutcome]") -> None:
            driven = self._driven.get(run.run_id)
            if driven is not None and driven[1] is task:
                del self._driven[run.run_id]
            if task.cancelled():
                # A task cancelled before it first ran never reached
                # _protected, so the run is ended here: cancelled, slot
                # released (RM17). A run that already ended is unchanged.
                run._abort(asyncio.CancelledError())
            else:
                # The run recorded its own failure (section 5), so the
                # exception has nothing left to report.
                task.exception()

        task = asyncio.get_running_loop().create_task(drive())
        task.add_done_callback(settle)
        self._driven[run.run_id] = (run, task)
        return self._started(run.run_id, task)

    def _started(self, run_id: str, task: "asyncio.Task[RunOutcome] | None" = None) -> StartedRun:
        record = self.store.get_run(run_id)
        return StartedRun(run_id, record.state, record.version, task)

    def _waiting_run(self, run_id: str, expected_version: int, wanted: RunState) -> "_Run":
        """The tracked run in the waiting state a resume expects (RM17, section 5).

        A terminal run never restarts (TerminalStateError), a stale
        version is 409 (StaleVersionError), a run in the other waiting
        state is 400 invalid_resume_input (ResumeInputError), and any
        other state, or a run this process does not track, is
        InvalidTransitionError.
        """
        record = self.store.get_run(run_id)
        state = RunState(record.state)
        tracked = self._waiting.get(run_id)
        if state not in WAITING_STATES or (
            tracked is not None and record.version != tracked.record.version
        ):
            self._drop_waiting(run_id)
        if state in TERMINAL_STATES:
            raise TerminalStateError(f"run {run_id} is {state}; terminal runs never restart")
        if record.version != expected_version:
            raise StaleVersionError(run_id, expected_version, record.version)
        if state is not wanted:
            if state in WAITING_STATES:
                raise ResumeInputError(_RESUME_MISMATCH[state])
            raise InvalidTransitionError(f"run {run_id} is {state}, not {wanted.value}")
        run = self._waiting.get(run_id)
        if run is None:
            raise InvalidTransitionError(
                f"run {run_id} is {state.value} but is not tracked by this gateway process"
            )
        return run

    def expire_waiting(self) -> list[RunOutcome]:
        """Fail every expired wait with input_expired or approval_expired (section 5).

        A clarification wait expires after waiting_seconds, a confirmation
        after its approval's five minutes (RM11).

        A run that left waiting_input some other way (for example, it was
        cancelled) is dropped without a transition: a terminal run never
        changes again (RM17). That holds when it is cancelled during its
        own expiry too: the failed transition is then not an error. An
        error on one run does not stop the sweep:
        the other waits are still expired, the run that failed stays
        tracked for the next sweep, and the errors are raised together at
        the end. The expired runs are recorded in the store either way.
        """
        expired: list[RunOutcome] = []
        errors: list[Exception] = []
        for run_id, run in list(self._waiting.items()):
            try:
                if not self._still_waiting(run_id):
                    self._drop_waiting(run_id)
                elif run.wait_expired():
                    expired.append(run.expire())
                    self._drop_waiting(run_id)
            except Exception as exc:
                if not self._still_waiting(run_id, default=True):
                    self._drop_waiting(run_id)
                else:
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("some expired waits could not be failed", errors)
        return expired

    def _drop_waiting(self, run_id: str) -> None:
        """Stop tracking a wait and release any unrevealed confirmation token (RM11)."""
        self._waiting.pop(run_id, None)
        self._issued.pop(run_id, None)

    def _still_waiting(self, run_id: str, *, default: bool = False) -> bool:
        """True when the store still shows the tracked wait for run_id.

        default is the answer when the store cannot be read.
        """
        try:
            record = self.store.get_run(run_id)
        except Exception:
            return default
        if RunState(record.state) not in WAITING_STATES:
            return False
        tracked = self._waiting.get(run_id)
        return tracked is None or record.version == tracked.record.version

    def _keep_if_waiting(self, run: "_Run", outcome: RunOutcome) -> RunOutcome:
        if outcome.state in WAITING_STATES:
            self._waiting[run.run_id] = run
            if outcome.confirmation is not None:
                self._issued[run.run_id] = outcome.confirmation
            else:
                self._issued.pop(run.run_id, None)
        else:
            self._drop_waiting(run.run_id)
        # A confirmation of a run that is no longer waiting can never be read.
        for run_id in [r for r in self._issued if r not in self._waiting]:
            del self._issued[run_id]
        return outcome


class _Run(_SelectionMixin, _GenerationMixin, _ToolsMixin, _ConfirmationMixin, _PublishMixin):
    """Composition root for one run while the orchestrator drives it (#296)."""

    def __init__(
        self,
        owner: LocalOrchestrator,
        messages: tuple[Message, ...],
        requested_mode: str | None,
        model_alias: str | None,
        final_options: GenerationOptions | None = None,
        idempotency: IdempotencyClaim | None = None,
        answer_stream_effective: str | None = None,
    ) -> None:
        self.answer_stream_effective = (
            "buffered"
            if model_alias == "relaymind-client-tools"
            else answer_stream_effective or owner.performance.answer_stream
        )
        if self.answer_stream_effective not in ("buffered", "live"):
            raise ValueError("invalid effective answer stream")
        self.o = owner
        self.idempotency = idempotency
        # The caller's answer options, laid over each answer generation's defaults.
        self.final_options = final_options or GenerationOptions()
        self.messages = messages
        self.requested_mode = requested_mode
        self.mode = ExecutionMode(requested_mode or ExecutionMode.AUTO)
        self.model_alias = model_alias
        # RM26: the class of the local model this run was accepted under; it keeps it.
        self.local = owner.local_status
        self.meter = BudgetMeter(
            owner.budgets,
            clock=owner.clock,
            local_active=(
                self.mode == ExecutionMode.LOCAL_ONLY
                or (self.mode != ExecutionMode.EXTERNAL_ONLY and self.local.usable)
            ),
        )
        self.model_calls: list[dict[str, str]] = []
        # Set when a cancel request stops the run (run.cancelled reason).
        # The adapter of every generation in flight, by call ID, for a cancel
        # (RM14). The parallel workflow has two at once (spec 0.25).
        self.generating: dict[str, ModelAdapter] = {}
        self.cancel_asked: set[str] = set()
        # The parallel workflow of this run, once it started (spec 0.25), and
        # the branch the tool events of the local branch are traced under.
        self.workflow: _Workflow | None = None
        self.tool_branch: str | None = None
        self.record: RunRecord
        self.decision: RouteDecision | None = None
        # The selector's narrowing of the selected skill's tools, intersected with its
        # allowlist; None when no selector narrowed (RM08, RM28).
        self.narrowed: tuple[str, ...] | None = None
        # Fixed by the first RouteDecision and kept across resumes (section 5).
        self.language: Language | None = None
        # The clarification being waited on, its start, and the waits so far.
        self.pending: _Pending | None = None
        # The held call of a run waiting for confirmation (RM11), and the
        # approval that authorized each executed proposal (trace evidence).
        self.confirmation: _Confirmation | None = None
        self._approvals: dict[str, str] = {}
        self.waiting_since: float | None = None
        self.waiting_ms = 0
        self.clarifications = 0
        # The run.accepted snapshot (section 8); RM07 pins skills to it.
        self.offered = owner.skills.offered_snapshot(owner.router.offered)
        # Secret values seen in tool results, scrubbed from model text (RM12).
        self.secrets: set[str] = set()
        self._calls = 0
        self.routing_placement = marker()
        self.pending_selector = None
        self.pending_placement = None
        self._routing_count = None
        # RM14: who may dispatch and who owns the final answer.
        self.authority = DispatchAuthority()
        # The skill the local loop loaded, and every tool result of the run.
        self.skill: Skill | None = None
        self.results: list[ToolResult] = []
        # Local proposals that never produced an operation, and rejected
        # drafts; a handoff carries them labeled as proposals (RM13).
        self.local_proposals: dict[str, dict[str, Any]] = {}
        self.drafts: list[dict[str, Any]] = []

    # -- trace helpers ---------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def active_model(self) -> ConfiguredModel:
        """The model that holds dispatch authority now (RM14)."""
        if self.authority.holder == EXTERNAL_ACTOR:
            assert self.o.external is not None
            return self.o.external
        # A run without a usable local model never generates before its handoff (RM26).
        assert self.o.model is not None
        return self.o.model

    @property
    def no_local(self) -> bool:
        """Whether this run is managed external execution (RM26): auto without a usable model."""
        return self.mode is ExecutionMode.AUTO and not self.local.usable

    def _local_label(self) -> dict[str, str]:
        """route.selected of a managed external route records the class as its cause (RM26)."""
        return {"local_model_status": self.local.status.value} if self.no_local else {}

    @property
    def actor(self) -> str:
        return self.active_model.actor

    def emit(self, event_type: EventType | str, actor: str, payload: Mapping[str, Any]) -> None:
        if event_type == EventType.ROUTE_SELECTED:
            payload = {"placement": self.routing_placement.to_json_object(), **payload}
        elif event_type in (EventType.HANDOFF_STARTED, EventType.RUN_FAILED):
            if self.pending_placement is not None:
                payload = {"placement": self.pending_placement.to_json_object(), **payload}
        payload = self._selector_payload(event_type, payload)
        self.o.store.append_event(self.run_id, EventType(event_type), actor, payload)
        self._selector_persisted(event_type)
        if event_type in (
            EventType.ROUTE_SELECTED,
            EventType.HANDOFF_STARTED,
            EventType.RUN_FAILED,
        ):
            # A decline is evidence for the next of these events only. Once the
            # route is recorded, a decline before it never explains a later stop.
            self.pending_placement = None

    def move(self, state: RunState, **kwargs: Any) -> None:
        event = kwargs.get("event")
        if event is not None and self.pending_placement is not None:
            kind, actor, payload = event
            if kind in (EventType.HANDOFF_STARTED, EventType.RUN_FAILED):
                kwargs["event"] = (
                    kind,
                    actor,
                    {"placement": self.pending_placement.to_json_object(), **payload},
                )
        if event is not None:
            kind, actor, payload = kwargs["event"]
            kwargs["event"] = (kind, actor, self._selector_payload(kind, payload))
        self.record = self.o.machine.transition(self.run_id, self.record.version, state, **kwargs)
        if event is not None:
            self._selector_persisted(event[0])
        if event is not None and event[0] in (EventType.HANDOFF_STARTED, EventType.RUN_FAILED):
            self.pending_placement = None

    def scrub(self, text: str) -> str:
        return redact_text(text, self.secrets)

    def redact_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Model-proposed arguments, redacted by key and of the run's secret values.

        Arguments are model text: a model that copies a secret it was
        shown into a tool call never writes it into the trace (RM12,
        spec 0.12 section 8).
        """
        return self.o.redactor.redact_with_repeats(dict(arguments), self.secrets)

    def _summary(self) -> dict[str, Any]:
        """The run.completed / run.failed evidence (RM16, RM22, section 12.1)."""
        usage = self.meter.usage()
        summary: dict[str, Any] = {
            "model_calls": list(self.model_calls),
            "external_calls": sum(1 for c in self.model_calls if c["actor"] == EXTERNAL_ACTOR),
            "budget_usage": usage,
            # Active and waiting time, reported separately.
            "active_ms": usage["active_ms"],
            "local_active_ms": usage["local_active_ms"],
            "waiting_ms": self.waiting_ms,
        }
        if self.workflow is not None:
            summary["workflow"] = self._workflow_summary(self.workflow)
        return summary

    def _workflow_summary(self, workflow: _Workflow) -> dict[str, Any]:
        """The measurement of the parallel workflow (RM15, RM25, spec 0.25 section 8).

        Every figure comes from the monotonic clock, recorded as the
        branches and calls ran; nothing is estimated.
        """
        stage_end = workflow.joined if workflow.joined is not None else self.o.clock.monotonic()
        return {
            "workflow_id": workflow.workflow_id,
            "branch_wall_ms": round((stage_end - workflow.started) * 1000),
            "local_health_ms": workflow.branch_ms.get(LOCAL_HEALTH, 0),
            "external_checklist_ms": workflow.branch_ms.get(EXTERNAL_CHECKLIST, 0),
            "extra_call_ms": sum(workflow.synthesis_ms) if workflow.synthesis_ms else None,
            "abandoned_calls": list(workflow.abandoned),
        }

    # -- the run ---------------------------------------------------------------

    async def execute(self) -> RunOutcome:
        self.open()
        return await self.drive()

    async def drive(self) -> RunOutcome:
        """Route and run an opened run to a terminal or waiting state."""
        return await self._guarded(self._route)

    def open(self) -> None:
        """Take the execution slot, record run.accepted, and enter routing.

        A mode the class of the local model does not permit is refused
        first, and no run is created (RM26, section 7.4).
        """
        refusal = self.local.refusal(self.mode)
        if refusal is not None:
            raise ExecutionModeForbiddenError(refusal)
        if self.o.machine.active_run_id() is not None:
            raise SlotUnavailableError("another run holds the execution slot (section 11)")
        # Section 5: from here on restart recovery must not run.
        self.o._accepted_a_run = True
        # RM22: run.accepted is written with the run in one transaction,
        # so an invalid payload creates no run, and a run can always fail
        # or be cancelled from accepted (section 5).
        with self.o.store.transaction():
            self.record = self.o.machine.create(
                accepted=(
                    EventType.RUN_ACCEPTED,
                    GATEWAY_ACTOR,
                    {
                        "requested_execution_mode": self.requested_mode,
                        "effective_execution_mode": self.mode.value,
                        "model_alias": self.model_alias,
                        "budgets": self._budget_record(),
                        **self.offered,
                        **({"routing_cache": True} if self.o.routing_cache is not None else {}),
                        **self._phase2_accepted(),
                        "original_messages": self._original_messages(),
                        "answer_options": self._answer_option_record(),
                    },
                ),
                idempotency=self.idempotency,
            )
            self.move(RunState.ROUTING)

    def _budget_record(self) -> dict[str, Any]:
        """The limits in force for run.accepted: section 11, and the selection limit (RM03)."""
        return {
            **self.o.budgets.to_json_object(),
            SELECTION_TIMEOUT_BUDGET: self.o.selection.timeout_seconds,
            "placement.margin": self.o.placement.settings.margin,
            "placement.probe_after_seconds": self.o.placement.settings.probe_after_seconds,
        }

    def _phase2_accepted(self) -> dict[str, Any]:
        """The phase 2 fields of run.accepted (spec 0.32 section 8), in one place.

        The performance options in force, the prefetch declarations of the
        offered skills (RM29), and the selector chain with the example_match
        thresholds (RM28); the placement and workflow fields of later
        changes are added here.
        """
        return {
            # RM26: the configured local model (null only when absent) and its class.
            **self.local.to_json_object(None if self.o.model is None else self.o.model.model_id),
            **self.o.performance.to_json_object(),
            "answer_stream_effective": self.answer_stream_effective,
            "prefetch_declarations": self.o.skills.prefetch_declarations(self.o.router.offered),
            # RM28: the selector chain in force, and the matcher thresholds (section 14.1).
            **self.o.selection.to_json_object(),
            "selection": list(self.o.selection_chain.names),
            "placement_policy": self.o.placement.settings.policy,
        }

    def _answer_option_record(self) -> dict[str, Any]:
        """The caller's answer options as applied, for run.accepted (spec 0.22 section 8)."""
        options = self.final_options
        record = {
            "max_completion_tokens": options.max_output_tokens,
            "temperature": options.temperature,
            "top_p": options.top_p,
        }
        return {name: value for name, value in record.items() if value is not None}

    def _original_messages(self) -> list[dict[str, Any]]:
        """The request's conversation text for run.accepted, redacted (spec 0.20 section 8).

        Tool messages and tool calls are not text of the request, so only
        the four conversation roles with content are kept.
        """
        roles = (Role.SYSTEM, Role.DEVELOPER, Role.USER, Role.ASSISTANT)
        kept = [
            {"role": m.role.value, "content": m.content}
            for m in self.messages
            if m.role in roles and m.content is not None
        ]
        return self.o.redactor.redact(kept)

    async def _guarded(self, route: Callable[[], Awaitable[Routing]]) -> RunOutcome:
        """Drive the run; any unexpected exception ends it (RM17, section 5)."""
        return await self._protected(lambda: self._drive(route))

    async def _protected(self, work: Callable[[], Awaitable[RunOutcome]]) -> RunOutcome:
        """Run work; any unexpected exception ends the run (RM17, section 5)."""
        try:
            return await work()
        except _LivePredicateFailure:
            return self._fail(
                "predicate_unmet", detail="the answer did not meet the completion criteria"
            )
        except BaseException as exc:
            self._abort(exc)
            raise

    async def _drive(self, route: Callable[[], Awaitable[Routing]]) -> RunOutcome:
        """Route, then wait, stop, or run the local loop to completion."""
        if self.mode is ExecutionMode.PARALLEL_DEMO:
            # Spec 0.17 section 4.4: no routing generation for a run that cannot succeed.
            if not self.o.enabled_workflows or self.o.external is None:
                return self._workflow_unavailable("configuration does not provide the workflow")
            if not await self._external_available():
                return self._workflow_unavailable("the external endpoint is not available")
        try:
            routing = await route()
            decision = self.decision = routing.decision
            if self.language is None:
                self.language = decision.language
            if decision.route is Route.CLARIFY:
                return self._clarify(routing)
            if (
                self.mode is ExecutionMode.PARALLEL_DEMO
                and decision.route is not Route.PARALLEL
                and decision.reason_code == PARALLEL_WORKFLOW
            ):
                # RM21: the skill's workflow is not enabled; parallel_demo
                # never hands the run off in its place (section 4.4).
                return self._workflow_unavailable(
                    "the skill's parallel workflow is not enabled by configuration"
                )
            if decision.route is Route.PARALLEL:
                return await self._run_parallel(decision)
            if decision.route is not Route.LOCAL:
                raise Escalation(
                    decision.reason_code, f"route {decision.route.value} needs external"
                )
            self.move(RunState.LOCAL_RUNNING)
            loop = self._local_setup(decision)
        except Escalation as stop:
            return await self._escalate(stop)
        except BudgetExceededError as exceeded:
            return await self._escalate(
                Escalation(exceeded.reason_code, str(exceeded), budget=exceeded.budget)
            )
        return await self._run_local(loop)

    async def _run_local(self, loop: _Loop, confirmed: _Confirmation | None = None) -> RunOutcome:
        """Run the local loop to an answer, a confirmation wait, or an escalation.

        confirmed is the held call of an approved confirmation: it runs
        first, once, and the loop continues from its result (RM11).
        """
        try:
            if confirmed is not None:
                await self._execute_step(
                    loop, confirmed.tool_call, confirmed.admitted, confirmed.proposal_id
                )
            else:
                # A new stage: its declared calls run before its first generation (RM29).
                await self._prefetch(loop)
            answer, answer_pass = await self._local_loop(loop)
        except _ConfirmationNeeded as needed:
            return self._await_confirmation(needed, None)
        except Escalation as stop:
            return await self._escalate(stop)
        except BudgetExceededError as exceeded:
            return await self._escalate(
                Escalation(exceeded.reason_code, str(exceeded), budget=exceeded.budget)
            )
        self.move(
            RunState.COMPLETED,
            event=(
                EventType.RUN_COMPLETED,
                GATEWAY_ACTOR,
                {
                    "final_answer_model": self.active_model.model_id,
                    "answer": answer,
                    # The pass that produced the answer (RM31, spec 0.32 section 8).
                    "answer_pass_applied": answer_pass,
                    **self._summary(),
                },
            ),
        )
        return RunOutcome(self.run_id, RunState.COMPLETED, answer=answer, decision=self.decision)

    # -- waiting for input (spec 0.6 sections 5, 8, 11) ------------------------------

    def _clarify(self, routing: Routing) -> RunOutcome:
        """Suspend in waiting_input, or fail once the clarification rounds are spent."""
        decision = routing.decision
        assert decision.clarification is not None
        if not routing.missing or not routing.missing.strip():
            raise ValueError("a clarification must name the input it waits for (section 8)")
        if self.clarifications >= self.o.budgets.clarification_rounds:
            return self._fail(
                MISSING_INPUT,
                reason_code=MISSING_INPUT,
                detail=(
                    f"a clarification beyond the {self.o.budgets.clarification_rounds} "
                    "allowed rounds was needed"
                ),
                missing=routing.missing,
            )
        self.clarifications += 1
        # Section 11: the stage ends; waiting is not active time.
        self.meter.end_stage()
        self.move(
            RunState.WAITING_INPUT,
            event=(
                EventType.CLARIFICATION_REQUIRED,
                GATEWAY_ACTOR,
                {
                    "question": decision.clarification,
                    "language": decision.language.value,
                    "reason_code": decision.reason_code,
                    "skill_id": decision.skill_id,
                    "missing": routing.missing,
                    "waiting_seconds": float(self.o.budgets.waiting_seconds),
                },
            ),
        )
        self.pending = _Pending(decision.clarification, routing.missing, decision.skill_id)
        self.waiting_since = self.o.clock.monotonic()
        return RunOutcome(
            self.run_id,
            RunState.WAITING_INPUT,
            clarification=decision.clarification,
            reason_code=decision.reason_code,
            decision=decision,
        )

    def reopen(self, expected_version: int) -> None:
        """Back to the state that suspended; reacquires the slot or raises 429 (section 5)."""
        self.record = self.o.machine.resume(self.run_id, expected_version)

    async def continue_with(self, text: str) -> RunOutcome:
        """Record the answer, open a new stage, and continue the run (section 5)."""

        async def route() -> Routing:
            pending = self.pending
            assert pending is not None
            # RM12, section 8: scrubbed once, for the trace and the conversation,
            # so a later handoff never carries the raw secret (RM13).
            answer = self.scrub(text)
            waited_ms = self._waited_ms()
            self.waiting_ms += waited_ms
            self.emit(
                EventType.CLARIFICATION_ANSWERED,
                GATEWAY_ACTOR,
                {"input": answer, "missing": pending.missing, "waited_ms": waited_ms},
            )
            self.pending = None
            self.waiting_since = None
            # Section 11: a new local stage with the full per-stage limits.
            self.meter.new_stage()
            self.messages = (
                *self.messages,
                Message(role=Role.ASSISTANT, content=pending.question),
                Message(role=Role.USER, content=answer),
            )
            if pending.skill_id is None:
                # No skill was chosen: route again with a routing generation.
                return await self._route()
            return self._resume_route(pending.skill_id)

        return await self._guarded(route)

    def _fail(self, error_code: str, *, detail: str, **extra: Any) -> RunOutcome:
        """End the run as failed with error_code and its run.failed event."""
        self.move(
            RunState.FAILED,
            error_code=error_code,
            event=(
                EventType.RUN_FAILED,
                GATEWAY_ACTOR,
                {"error_code": error_code, "detail": detail, **extra, **self._summary()},
            ),
        )
        return RunOutcome(
            self.run_id,
            RunState.FAILED,
            error_code=error_code,
            reason_code=extra.get("reason_code"),
            decision=self.decision,
            budget=extra.get("budget"),
        )

    async def _external_available(self) -> bool:
        """Whether the configured external endpoint reports itself available (section 4.4).

        A health check that fails counts as unavailable; its text may
        carry a URL or a key, so it is not recorded (RM12).
        """
        external = self.o.external
        assert external is not None
        try:
            health = await external.adapter.health()
        except Exception:
            return False
        return health.available

    def _workflow_unavailable(self, detail: str) -> RunOutcome:
        """parallel_demo fails explicitly when its workflow cannot run (RM21, section 4.4)."""
        return self._fail(WORKFLOW_UNAVAILABLE, detail=detail, reason_code=PARALLEL_WORKFLOW)

    def _needs_external(self, stop: Escalation) -> RunOutcome:
        self.move(
            RunState.FAILED,
            error_code=NEEDS_EXTERNAL,
            event=(
                EventType.RUN_FAILED,
                GATEWAY_ACTOR,
                {
                    "error_code": NEEDS_EXTERNAL,
                    "reason_code": stop.reason_code,
                    "detail": stop.detail,
                    **stop.extra,
                    **self._summary(),
                },
            ),
        )
        return RunOutcome(
            self.run_id,
            RunState.FAILED,
            error_code=NEEDS_EXTERNAL,
            reason_code=stop.reason_code,
            decision=self.decision,
            budget=stop.extra.get("budget"),
        )

    def _abort(self, exc: BaseException) -> None:
        """End a run an unexpected exception interrupted (RM17, section 5).

        The run fails with internal_error, or is cancelled when the task
        was cancelled; either way the slot is released. Only the
        exception type is recorded: a message may carry data (RM12). A
        failure to record this never hides the original exception.
        The exception is marked with the run, so the API reports it as
        internal_error, never as an error of the request (section 11).
        """
        try:
            setattr(exc, _ABORTED_RUN, self.run_id)
        except Exception:
            pass
        try:
            self.record = self.o.machine.get(self.run_id)
            if RunState(self.record.state) in TERMINAL_STATES:
                return
            if isinstance(exc, asyncio.CancelledError):
                # A cancel that reaches the task, from the native cancel,
                # a chat disconnect, or the event loop, is a cancel
                # request (spec 0.17 sections 7.4 and 8).
                payload = self._selector_payload(
                    EventType.RUN_CANCELLED, {"reason": CANCEL_REQUESTED, **self._summary()}
                )
                self.record = self.o.machine.cancel(
                    self.run_id,
                    self.record.version,
                    reason=CANCEL_REQUESTED,
                    event=(EventType.RUN_CANCELLED, GATEWAY_ACTOR, payload),
                )
                self._selector_persisted(EventType.RUN_CANCELLED)
                return
            self.move(
                RunState.FAILED,
                error_code=INTERNAL_ERROR,
                event=(
                    EventType.RUN_FAILED,
                    GATEWAY_ACTOR,
                    {
                        "error_code": INTERNAL_ERROR,
                        "detail": type(exc).__name__,
                        **self._summary(),
                    },
                ),
            )
        except Exception:
            pass

    # -- escalation and the external stage (RM04, RM13, RM14) ----------------------

    def _local_unavailable(self, detail: str) -> Escalation:
        """The stop for an unavailable local model (section 11).

        It hands off only when the operator policy permits degraded mode.
        """
        return Escalation(
            "capability_missing", detail, hand_off=self.o.degraded_mode, degraded=True
        )

    async def _escalate(self, stop: Escalation) -> RunOutcome:
        """Hand the run to the external model, or end it as needs_external (RM04).

        External dispatch needs a configured external model, an effective
        mode other than local_only, and room in the entire-run limits. An
        operation with no known outcome blocks the handoff (RM14). The
        envelope is measured against the external context limit before
        anything is sent (RM13). A stop the external stage could not
        resolve either does not hand off (section 4.4).
        """
        if self.authority.answer_owner is not None:
            return self._fail(
                EXTERNAL_FAILED, detail=stop.detail, reason_code=stop.reason_code, **stop.extra
            )
        external = self.o.external
        if external is None or self.mode is ExecutionMode.LOCAL_ONLY or not stop.hand_off:
            return self._needs_external(stop)
        operations = self.o.store.tool_operations(self.run_id)
        blocked = self._handoff_blocked(stop, operations)
        if blocked is not None:
            return blocked
        if stop.degraded:
            self._record_degraded_route()
        # The external stage works from the recorded evidence the envelope
        # carries, not from what the local loop kept: an operation can be
        # recorded just before a local stop (RM05, RM13).
        self.results[:] = [ToolResult.from_json_object(op["result"]) for op in operations]

        skill = self.skill
        # RM29: an external stage that loads the skill itself is its first skill stage,
        # so the skill's declared calls run before its first generation.
        first_stage = False
        if skill is None and self.decision is not None and self.decision.skill_id is not None:
            skill = self.skill = self._load_skill(self.decision.skill_id)
            first_stage = True
        if skill is not None:
            allowlist: tuple[str, ...] = self._skill_allowlist(skill)
            catalog: tuple[tuple[str, str], ...] = ()
        else:
            offered = [self.o.skills.get(name) for name in self.o.router.offered]
            allowlist = tuple(dict.fromkeys(t for s in offered for t in s.allowed_tools))
            catalog = tuple((s.name, s.description) for s in offered)
        available, schemas = self._permitted_tools(allowlist)
        if skill is not None and not schemas:
            # RM08: the external stage gets the same permitted tools, none,
            # so it could not collect the evidence the skill needs.
            return self._needs_external(
                Escalation("capability_missing", f"no permitted tools for skill {skill.name!r}")
            )
        language = self.language or request_language(self.messages)
        done = {op["tool_name"] for op in operations}
        pending: list[dict[str, Any]] = (
            [{"kind": "tool_call", "tool_name": s.name} for s in schemas if s.name not in done]
            if skill is not None
            else []
        )
        pending.append({"kind": "final_answer", "tool_name": None})
        envelope = build_handoff_envelope(
            run_id=self.run_id,
            messages=self.messages,
            skill=None
            if skill is None
            else {"name": skill.name, "version": skill.version, "content_hash": skill.content_hash},
            operations=operations,
            proposals=[*self.local_proposals.values(), *self.drafts],
            allowed_tools=[s.name for s in schemas],
            pending_work=pending,
            reason_code=stop.reason_code,
            remaining_budgets=self.meter.remaining_budgets(),
            language=language.value,
        )
        criteria = None if skill is None else COMPLETION_CRITERIA[skill.completion_predicate]
        conversation = list(
            external_messages(
                envelope,
                skill_body=None if skill is None else skill.body,
                catalog=catalog,
                answer_instruction=ANSWER_LANGUAGE[language],
                completion_criteria=() if criteria is None else criteria.fixed,
            )
        )
        predicate = None if skill is None else COMPLETION_PREDICATES[skill.completion_predicate]
        loop = _Loop(
            conversation,
            schemas,
            available,
            skill,
            predicate,
            actor=EXTERNAL_ACTOR,
            criteria=criteria,
            language=language.value,
        )
        # The first request is measured with the values message, if results exist (RM13).
        self._refresh_criteria(loop)
        extra: dict[str, Any] = (
            {"degraded_mode": True, "local_failure": self.scrub(stop.detail)}
            if stop.degraded
            else {}
        )
        if "budget" in stop.extra:
            # Spec 0.48 section 8: the limit that stopped the local stage (section 14.4.7).
            extra["budget"] = stop.extra["budget"]
        return await self._hand_off(external, envelope, loop, stop, extra, prefetch=first_stage)

    def _handoff_blocked(
        self, stop: Escalation, operations: Sequence[Mapping[str, Any]]
    ) -> RunOutcome | None:
        """End the run as needs_external when handoff cannot start, or return None (RM04, RM14).

        Handoff needs room in the entire-run limits; an operation with no
        known outcome blocks it.
        """
        room = self.meter.external_room()
        if room is not None:
            return self._needs_external(
                Escalation(
                    stop.reason_code, stop.detail, **{**stop.extra, "dispatch_blocked": room.budget}
                )
            )
        unsettled = unsettled_operations(operations)
        if unsettled:
            blocker = unsettled[0]
            return self._needs_external(
                Escalation(
                    "tool_failure",
                    f"operation {blocker['operation_id']} has no known outcome; it blocks handoff",
                    operation_id=blocker["operation_id"],
                    tool_status=blocker["status"],
                )
            )
        return None

    async def _hand_off(
        self,
        external: ConfiguredModel,
        envelope: HandoffEnvelope,
        loop: _Loop,
        stop: Escalation,
        extra: Mapping[str, Any],
        capabilities: AdapterCapabilities | None = None,
        *,
        prefetch: bool = False,
    ) -> RunOutcome:
        """Measure the request, record handoff.started, and run the external stage (RM13, RM14).

        The request is measured against the external context limit before
        anything is sent. extra adds payload fields to handoff.started.
        prefetch runs the skill's declared calls first (RM29).
        """
        try:
            if capabilities is None:
                capabilities = await external.adapter.capabilities()
            if loop.schemas:
                capabilities.require("tool_calling")
            size = await measure_request(
                external.adapter, capabilities, loop.conversation, loop.schemas
            )
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            return self._fail(
                EXTERNAL_FAILED,
                detail=f"the external request could not be prepared: {exc}",
                reason_code=stop.reason_code,
            )
        if not size.fits:
            return self._fail_context(size)
        local_boundary = self.o.clock.monotonic()
        self.move(
            RunState.EXTERNAL_RUNNING,
            event=(
                EventType.HANDOFF_STARTED,
                GATEWAY_ACTOR,
                {
                    "reason_code": stop.reason_code,
                    "local_active_ms": self.meter.local_active_ms(local_boundary),
                    "model": external.model_id,
                    "skill": None if envelope.skill is None else dict(envelope.skill),
                    "operation_ids": list(envelope.operation_ids),
                    "allowed_tools": list(envelope.allowed_tools),
                    "request": size.to_json_object(),
                    **extra,
                },
            ),
        )
        # RM14: from here only the external actor dispatches or answers.
        self.authority.hand_off_to_external()
        self.meter.close_local(local_boundary)
        self.meter.start_external()
        self.tool_branch = None
        loop.capabilities = capabilities
        return await self._run_external(loop, stop, prefetch=prefetch)

    def _record_degraded_route(self) -> None:
        """Before any routing decision exists, the gateway records its own (section 11).

        A clarify decision belongs to the stage before the wait: a
        resumed stage that failed while routing again has no decision
        yet (section 5).
        """
        if self.decision is not None and self.decision.route is not Route.CLARIFY:
            return
        decision = self.o.router.degraded_decision(self.messages, self.mode, language=self.language)
        self.decision = decision
        self.language = decision.language
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {
                "decision": decision.to_json_object(),
                "mechanism": "degraded_mode",
                **self._cache_label("miss"),
            },
        )

    async def _run_external(
        self,
        loop: _Loop,
        stop: Escalation,
        confirmed: _Confirmation | None = None,
        *,
        prefetch: bool = False,
    ) -> RunOutcome:
        """Run the external loop to an answer, a confirmation wait, or a failure (RM13, RM14).

        confirmed is the held call of an approved confirmation: it runs
        first, once, and the loop continues from its result (RM11). stop
        is the escalation that started this stage; its reason code stays
        on every external failure. prefetch runs the skill's declared
        calls before the first generation (RM29); the grown request is
        measured again.
        """
        external = self.o.external
        assert external is not None
        try:
            if confirmed is not None:
                await self._execute_step(
                    loop, confirmed.tool_call, confirmed.admitted, confirmed.proposal_id
                )
                await self._check_external_size(loop)
            elif prefetch:
                before = len(loop.conversation)
                await self._prefetch(loop)
                if len(loop.conversation) != before:
                    await self._check_external_size(loop)
            answer = await self._external_loop(loop)
        except _ConfirmationNeeded as needed:
            return self._await_confirmation(needed, stop)
        except ContextLimitExceeded as exceeded:
            return self._fail_context(exceeded.size)
        except ExternalFailure as failure:
            return self._fail(
                EXTERNAL_FAILED,
                detail=failure.detail,
                reason_code=stop.reason_code,
                **failure.extra,
            )
        except Escalation as stopped:
            # A stop inside the external stage has no further escalation (RM14).
            return self._fail(
                EXTERNAL_FAILED,
                detail=stopped.detail,
                reason_code=stopped.reason_code,
                **stopped.extra,
            )
        except BudgetExceededError as exceeded:
            return self._fail(
                EXTERNAL_FAILED,
                detail=str(exceeded),
                reason_code=stop.reason_code,
                budget=exceeded.budget,
            )
        self.move(
            RunState.COMPLETED,
            event=(
                EventType.RUN_COMPLETED,
                GATEWAY_ACTOR,
                {"final_answer_model": external.model_id, "answer": answer, **self._summary()},
            ),
        )
        return RunOutcome(self.run_id, RunState.COMPLETED, answer=answer, decision=self.decision)

    def _fail_context(self, size: RequestSize) -> RunOutcome:
        """Evidence over the external context budget fails explicitly (RM13)."""
        return self._fail(
            CONTEXT_LIMIT_EXCEEDED,
            reason_code="context_limit",
            detail=(
                f"the external request needs {size.size} ({size.method}) "
                f"and the external context limit is {size.limit}"
            ),
            request=size.to_json_object(),
        )

    async def _external_loop(self, loop: _Loop) -> str:
        """The external model's bounded loop: propose tools, then answer (RM13, RM14).

        The caller measured the first request. Tool proposals take the
        local path's checks and the same executor; a mutating tool waits
        for a confirmation (RM11). A proposal that repeats a completed
        operation is answered from the record and runs no handler
        (RM10). The answer is buffered, checked against the skill's
        completion predicate, and published once (RM20).
        """
        offered = tuple(t.name for t in loop.schemas)
        while True:
            eligible = (
                self.answer_stream_effective == "live"
                and loop.skill is not None
                and pending_evidence_complete(
                    offered, loop.skill.completion_predicate, self.results
                )
            )
            schemas = () if eligible else loop.schemas
            _, generation = await self._generate(
                "external",
                loop.conversation,
                schemas,
                self._answer_options(EXTERNAL_OPTIONS, tools_offered=bool(schemas)),
                branch=loop.branch,
                answer_predicate=loop.skill.completion_predicate if eligible else None,
            )
            if generation.calls:
                turn = self._tool_turn(generation, loop.available, loop.skill)
                if turn.rejection is not None:
                    loop.conversation += turn.rejection_exchange()
                else:
                    await self._tool_step(loop, turn)
            elif (
                not generation.rejected
                and generation.text.strip()
                and (loop.predicate is None or loop.predicate(generation.text, self.results))
            ):
                return self._publish(generation, EXTERNAL_ACTOR)
            else:
                loop.conversation += _external_correction(loop, generation.text, self.results)
            await self._check_external_size(loop)

    def _refresh_criteria(self, loop: _Loop) -> None:
        """Keep one gateway rules message with the criteria values of the latest results.

        The values come from the latest successful tool results only (RM12).
        The message moves to the end of the conversation before each external
        generation, behind any tool result, rejected answer, or correction
        added since, and replaces its earlier copy, so a request never carries
        two (spec 0.35 section 4.4). There is none when no successful result
        exists.
        """
        if loop.criteria is None:
            return
        lines = loop.criteria.values(self.results, loop.language)
        current = loop.criteria_message
        if current is not None:
            loop.conversation[:] = [m for m in loop.conversation if m is not current]
            loop.criteria_message = None
        if lines:
            content = "\n".join([CRITERIA_VALUES_HEADER, *(f"- {line}" for line in lines)])
            loop.criteria_message = Message(role=Role.SYSTEM, content=content)
            loop.conversation.append(loop.criteria_message)

    async def _check_external_size(self, loop: _Loop) -> None:
        """Refresh the criteria values, then measure the next request (RM13).

        Tool results accumulate, so the request is measured again before
        every external generation, with the values message it will carry.
        """
        self._refresh_criteria(loop)
        assert loop.capabilities is not None
        try:
            size = await measure_request(
                self.active_model.adapter, loop.capabilities, loop.conversation, loop.schemas
            )
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            # Section 11: an external stage that cannot finish is external_failed.
            raise ExternalFailure(f"the external request could not be measured: {exc}") from exc
        if not size.fits:
            raise ContextLimitExceeded(size)

    # -- the parallel workflow (spec 0.25 section 4.4; RM14, RM15, RM16) ----------------

    async def _run_parallel(self, decision: RouteDecision) -> RunOutcome:
        """Run the fixed workflow: two read-only branches, the join, the synthesis call.

        The local branch collects the observation with the skill's
        read-only tool; the external branch drafts a checklist from the
        request alone. Neither sees the other: nothing is injected into a
        running generation (RM15). When both have finished, the gateway
        hands final ownership to a new external call (RM14, RM20), which
        combines the observation and the provisional draft. A failure of
        one branch cancels the other, and the calls of both stay in the
        inventory of the terminal event (section 12.1).
        """
        external = self.o.external
        if external is None:
            raise Escalation(PARALLEL_WORKFLOW, "no external model is configured", hand_off=False)
        # Branches are read-only (spec 0.25): a mutating tool is never offered to them.
        loop = self._local_setup(decision, read_only=True)
        skill = loop.skill
        assert skill is not None and skill.parallel_workflow is not None
        # The workflow needs the local branch generation and two external
        # generations, the draft and the synthesis (section 11).
        room = self.meter.external_room(external_generations=2, local_generations=1)
        if room is not None:
            raise Escalation(
                room.reason_code,
                str(room),
                hand_off=False,
                budget=room.budget,
                dispatch_blocked=room.budget,
            )
        # The draft request is measured before any branch starts (RM13).
        assert self.language is not None
        draft_messages = (
            Message(
                role=Role.SYSTEM, content=f"{DRAFT_INSTRUCTION}\n{ANSWER_LANGUAGE[self.language]}"
            ),
            *self.messages,
        )
        try:
            capabilities = await external.adapter.capabilities()
            size = await measure_request(external.adapter, capabilities, draft_messages, ())
        except (UnsupportedCapabilityError, TokenCountError) as exc:
            return self._fail(
                EXTERNAL_FAILED,
                detail=f"the draft request could not be measured: {exc}",
                reason_code=PARALLEL_WORKFLOW,
            )
        if not size.fits:
            return self._fail_context(size)
        self.move(RunState.PARALLEL_RUNNING)
        self.meter.mark_local_unsupported()
        workflow = self.workflow = _Workflow(skill.parallel_workflow, self.o.clock.monotonic())
        self.tool_branch = LOCAL_HEALTH
        tool_names = " or ".join(schema.name for schema in loop.schemas)
        local = asyncio.create_task(
            self._timed_branch(workflow, LOCAL_HEALTH, self._local_health_branch(loop, tool_names))
        )
        draft = asyncio.create_task(
            self._timed_branch(
                workflow, EXTERNAL_CHECKLIST, self._checklist_branch(external, draft_messages)
            )
        )
        try:
            await asyncio.wait({local, draft}, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            # A cancel or a failed branch settles the other one before anything else.
            unfinished = [task for task in (local, draft) if not task.done()]
            if unfinished:
                self.cancel_generation()
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
        failed = self._failed_branch(local, draft)
        if failed is not None:
            return failed
        workflow.joined = self.o.clock.monotonic()
        return await self._synthesize(skill, draft.result(), external, capabilities)

    async def _timed_branch(self, workflow: _Workflow, branch: str, work: Awaitable[Any]) -> Any:
        """Await one branch and record how long it ran, however it ended (RM25)."""
        started = self.o.clock.monotonic()
        try:
            return await work
        finally:
            workflow.branch_ms[branch] = round((self.o.clock.monotonic() - started) * 1000)

    def _failed_branch(
        self, local: "asyncio.Task[Any]", draft: "asyncio.Task[Any]"
    ) -> RunOutcome | None:
        """End the run for the branch that failed, or None when both finished (section 4.4)."""
        for task in (local, draft):
            exc = None if task.cancelled() else task.exception()
            if exc is None:
                continue
            if isinstance(exc, ContextLimitExceeded):
                return self._fail_context(exc.size)
            if task is local:
                if isinstance(exc, Escalation):
                    # The workflow never hands the run to one external actor.
                    return self._needs_external(exc)
                if isinstance(exc, BudgetExceededError):
                    return self._needs_external(
                        Escalation(exc.reason_code, str(exc), hand_off=False, budget=exc.budget)
                    )
            elif isinstance(exc, ExternalFailure):
                return self._fail(
                    EXTERNAL_FAILED,
                    detail=exc.detail,
                    reason_code=PARALLEL_WORKFLOW,
                    **exc.extra,
                )
            elif isinstance(exc, BudgetExceededError):
                return self._fail(
                    EXTERNAL_FAILED,
                    detail=str(exc),
                    reason_code=PARALLEL_WORKFLOW,
                    budget=exc.budget,
                )
            raise exc
        return None

    async def _local_health_branch(self, loop: _Loop, tool_names: str) -> ToolResult:
        """The local branch: one read-only tool call, validated and executed (RM05, RM09, RM10).

        It ends with the tool result and writes no answer. A text answer or
        an invalid proposal gets the one format repair the local limits
        allow, then stops (RM04).
        """
        skill = loop.skill
        while True:
            _, generation = await self._generate(
                loop.purpose, loop.conversation, loop.schemas, branch=LOCAL_HEALTH
            )
            loop.purpose = "skill"
            if generation.calls:
                # The fixed workflow's branch makes one call, whatever the option says (RM15).
                turn = self._tool_turn(generation, loop.available, skill, many=False)
                if turn.rejection is not None:
                    loop.purpose = self._repair_or_stop(turn.rejection)
                    loop.conversation += turn.rejection_exchange()
                    continue
                await self._execute_step(
                    loop, turn.calls[0], turn.admitted[0], turn.proposal_ids[0]
                )
                return self.results[-1]
            self._keep_draft(generation.text)
            problem = {
                "code": "tool_call_missing" if generation.text.strip() else "empty_output",
                "message": "the local branch must call a tool, not write an answer",
            }
            loop.purpose = self._repair_or_stop(problem)
            loop.conversation += [
                Message(role=Role.ASSISTANT, content=generation.text or "(no answer)"),
                Message(role=Role.USER, content=BRANCH_CORRECTION.format(tools=tool_names)),
            ]

    async def _checklist_branch(
        self, external: ConfiguredModel, messages: Sequence[Message]
    ) -> str:
        """The external branch: a provisional checklist drafted from the request alone (RM15).

        The caller measured the request against the external context
        limit before the branches started (RM13). The draft is not tool
        evidence and is never published as the answer.
        """
        _, generation = await self._generate(
            "draft", messages, (), EXTERNAL_OPTIONS, model=external, branch=EXTERNAL_CHECKLIST
        )
        if generation.calls or not generation.text.strip():
            raise ExternalFailure("the checklist draft was blank or proposed a tool call")
        return self.scrub(generation.text)

    async def _synthesize(
        self,
        skill: Skill,
        draft: str,
        external: ConfiguredModel,
        capabilities: AdapterCapabilities,
    ) -> RunOutcome:
        """The join: transfer final ownership and run the new external call (RM14, RM15, RM20).

        The request has the health tool result as data and the draft as an
        unverified provisional proposal in a data message, JSON-encoded so
        its text cannot pass for gateway rules (RM12); the answer must meet
        the skill's completion predicate (RM16).
        """
        assert skill.parallel_workflow is not None
        stop = Escalation(PARALLEL_WORKFLOW, "synthesis of the parallel workflow", hand_off=False)
        operations = self.o.store.tool_operations(self.run_id)
        blocked = self._handoff_blocked(stop, operations)
        if blocked is not None:
            return blocked
        language = self.language or request_language(self.messages)
        envelope = build_handoff_envelope(
            run_id=self.run_id,
            messages=self.messages,
            skill={
                "name": skill.name,
                "version": skill.version,
                "content_hash": skill.content_hash,
            },
            operations=operations,
            proposals=[],
            allowed_tools=[],
            pending_work=[{"kind": "final_answer", "tool_name": None}],
            reason_code=PARALLEL_WORKFLOW,
            remaining_budgets=self.meter.remaining_budgets(),
            language=language.value,
        )
        base = external_messages(
            envelope,
            skill_body=skill.body,
            catalog=(),
            answer_instruction=ANSWER_LANGUAGE[language],
        )
        conversation = [
            base[0],
            Message(
                role=Role.SYSTEM,
                content=SYNTHESIS_INSTRUCTION.format(workflow=skill.parallel_workflow),
            ),
            *base[1:],
            Message(
                role=Role.USER,
                content=DRAFT_HEADER
                + "\n"
                + json.dumps({"provisional_draft": draft}, ensure_ascii=False),
            ),
        ]
        loop = _Loop(
            conversation,
            (),
            frozenset(),
            skill,
            COMPLETION_PREDICATES[skill.completion_predicate],
            actor=EXTERNAL_ACTOR,
            branch=SYNTHESIS,
        )
        return await self._hand_off(
            external, envelope, loop, stop, {"workflow": skill.parallel_workflow}, capabilities
        )

    async def _local_loop(self, loop: _Loop) -> tuple[str, str]:
        """The local generation loop: propose tools, then finalize (RM02, RM03, RM09).

        Returns the published answer and the pass that produced it,
        draft or final (RM31).
        """
        skill = loop.skill
        predicate = loop.predicate
        assert skill is not None and predicate is not None
        results = self.results

        while True:
            _, generation = await self._generate(loop.purpose, loop.conversation, loop.schemas)
            loop.purpose = "skill"

            if generation.calls:
                turn = self._tool_turn(generation, loop.available, skill)
                if turn.rejection is not None:
                    loop.purpose = self._repair_or_stop(turn.rejection)
                    loop.conversation += turn.rejection_exchange()
                    continue
                await self._tool_step(loop, turn)
                continue

            # A text answer is a finalize proposal (section 7.3).
            if generation.text.strip() and predicate(generation.text, results):
                if self._draft_publishable(generation.text):
                    # RM31: the checked draft is the answer; no second generation.
                    return self._publish(generation, LOCAL_ACTOR), "draft"
                return await self._final(loop.conversation, predicate, results), "final"
            self._keep_draft(generation.text)
            problem = {
                "code": "completion_predicate_unmet" if generation.text.strip() else "empty_output",
                "message": (
                    f"the answer does not meet the skill's completion criteria "
                    f"({skill.completion_predicate})"
                ),
            }
            loop.purpose = self._repair_or_stop(problem)
            loop.conversation += _correction_exchange(generation.text)


def _stop(run: _Run, task: "asyncio.Task[Any]") -> None:
    """Stop a run its task is driving, as the native cancel does (RM14, AC12).

    Spec 0.19 section 7.4: the run task cannot dispatch anything until
    the caller yields, so asking the adapter first still stops dispatch
    first, and the adapter is asked while it still knows the generation
    (task.cancel() tears its stream down).
    """
    run.cancel_generation()
    task.cancel()


__all__ = [
    "APPROVAL_EXPIRED",
    "CONTEXT_LIMIT_EXCEEDED",
    "CORRECTION",
    "CRITERIA_VALUES_HEADER",
    "EXTERNAL_FAILED",
    "EXTERNAL_OPTIONS",
    "GATEWAY_ACTOR",
    "INPUT_EXPIRED",
    "INTERNAL_ERROR",
    "LOCAL_OPTIONS",
    "MISSING_INPUT",
    "NEEDS_EXTERNAL",
    "AcceptedRun",
    "ApprovalRejectedError",
    "ConfiguredModel",
    "ConfirmationRequest",
    "ContextLimitExceeded",
    "Escalation",
    "ExecutionModeForbiddenError",
    "ExternalFailure",
    "LocalOrchestrator",
    "ResumeInputError",
    "RunOutcome",
    "StartedRun",
    "aborted_run_id",
    "not_executed_rejection",
    "skill_stage_messages",
]
