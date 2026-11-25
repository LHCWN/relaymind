"""Model generation, placement admission, streaming, and timing records (RM03, RM27, RM32; #296)."""

import asyncio
import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from typing import Any

from relaymind.adapters import (
    Completed,
    FirstToken,
    GenerationFailed,
    GenerationOptions,
    GenerationRequest,
    Message,
    TextDelta,
    ToolCall,
    ToolSchema,
    Usage,
)
from relaymind.orchestrator._run_types import (
    LOCAL_OPTIONS,
    SYNTHESIS,
    ConfiguredModel,
    Escalation,
    ExternalFailure,
    _Generation,
    _LivePredicateFailure,
    _PublicationFailure,
    _Workflow,
)
from relaymind.orchestrator.budgets import EXTERNAL_PURPOSES, BudgetExceededError
from relaymind.orchestrator.placement import PlacementDeclined, marker
from relaymind.skills.predicates import StatementGate, StatementRejected
from relaymind.store import ModelTiming
from relaymind.store.model_timings import observed_cached_tokens
from relaymind.store.trace_event import EventType


class _GenerationMixin:
    """Generation execution, placement admission, cancellation, and timing mixed into _Run."""

    def cancel_generation(self) -> None:
        """Ask the adapter of the generation in flight, if any, to cancel it (RM14, AC12)."""
        for call_id in list(self.generating):
            self._cancel_call(call_id)

    def _cancel_call(self, call_id: str) -> None:
        """Fence deadline, branch and user cancellation to one request per live call."""
        adapter = self.generating.get(call_id)
        if adapter is not None and call_id not in self.cancel_asked:
            self.cancel_asked.add(call_id)
            self.o.ask_provider_to_cancel(adapter, call_id)

    async def _admit_local_placement(
        self,
        model: ConfiguredModel,
        purpose: str,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema],
        options: GenerationOptions,
    ) -> Any:
        """Count tokens (reusing routing measurement when identical) and check placement."""
        tokens = None
        measured = self._routing_count
        if measured is not None and (tuple(messages), tuple(tool_schemas)) == measured[:2]:
            tokens = measured[2]
        else:
            try:
                async with asyncio.timeout(
                    max(self.meter.remaining_seconds(), 0.001)
                ) as token_timeout:
                    tokens = await model.adapter.count_tokens(messages, tool_schemas)
            except TimeoutError:
                if token_timeout.expired():
                    raise self.meter.time_exhausted() from None
                # The adapter's own tokenizer timeout is an unavailable measurement,
                # not evidence that the gateway's active-time budget was spent.
                tokens = None
            except Exception:
                # Counting solely for placement must not create a capability failure.
                tokens = None
            else:
                if token_timeout.expired():
                    # A tokenizer that suppressed cancellation still used its budget.
                    raise self.meter.time_exhausted()
        self.meter.check_generation(purpose)
        prediction = self.o.placement.predict(
            model.model_id, purpose, tokens, output_cap=options.max_output_tokens
        )
        placement = self.o.placement.admit(
            prediction,
            mode=self.mode.value,
            external_model=None if self.o.external is None else self.o.external.model_id,
            stage_seconds=self.o.budgets.active_seconds - self.meter.elapsed_seconds(),
            run_seconds=self.o.budgets.run_active_seconds - self.meter.active_seconds(),
            skill_id=None if self.skill is None else self.skill.name,
        )
        if purpose == "routing":
            self.routing_placement = placement
        if placement.role == "none":
            if purpose == "routing":
                # route.selected carries a routing decline (section 14.2.2);
                # a later handoff or failure never does.
                raise PlacementDeclined(placement)
            self.pending_placement = placement
            raise Escalation("local_slow", "local generation declined by placement")
        return placement

    async def _generate(
        self,
        purpose: str,
        messages: Sequence[Message],
        tool_schemas: Sequence[ToolSchema],
        options: GenerationOptions = LOCAL_OPTIONS,
        *,
        model: ConfiguredModel | None = None,
        branch: str | None = None,
        answer_predicate: str | None = None,
    ) -> tuple[str, _Generation]:
        """Charge, run, and trace one model call (RM03, RM16); the only budget path."""
        if self.authority.answer_owner is not None:
            raise RuntimeError("an answer generation already owns this run")
        model = model or self.active_model
        dispatch_role = self.authority.holder
        on_external = purpose in EXTERNAL_PURPOSES
        label = {} if branch is None else {"branch": branch}
        self.meter.check_generation(purpose)
        placement = (
            await self._admit_local_placement(model, purpose, messages, tool_schemas, options)
            if model.actor == "local"
            else marker(external=True)
        )
        self.meter.charge_generation(purpose)
        self._calls += 1
        call_id = f"{self.run_id}_gen{self._calls}_{purpose}"
        request = GenerationRequest(
            generation_id=call_id,
            messages=tuple(messages),
            tool_schemas=tuple(tool_schemas),
            options=options,
        )
        model_id = model.model_id
        actor = model.actor
        call = {"call_id": call_id, "model": model_id, "actor": actor, "purpose": purpose}
        self.model_calls.append(call)
        declared = getattr(model.adapter, "request_options", None)
        sent = {"request_options": dict(declared)} if declared else {}
        self.emit(
            EventType.MODEL_STARTED,
            actor,
            {
                **call,
                "tools_offered": [t.name for t in request.tool_schemas],
                **sent,
                **label,
                "placement": placement.to_json_object(),
            },
        )
        started = self.o.clock.monotonic()
        first_token: float | None = None
        generation = _Generation(call_id=call_id)
        gate = (
            StatementGate(answer_predicate, self.results, hold=bool(self.secrets))
            if self.answer_stream_effective == "live" and answer_predicate is not None
            else None
        )
        if gate is not None and tool_schemas:
            raise RuntimeError("a live answer cannot offer tools")
        usage: Usage | None = None
        finish: str | None = None
        completed = False
        gating = False
        failure: GenerationFailed | None = None
        exhausted: BudgetExceededError | None = None
        deadline_missed = False
        limit = self.meter.remaining_seconds(on_external)
        placement_bound = False
        if placement.deadline_seconds is not None:
            run_left = self.o.budgets.run_active_seconds - self.meter.active_seconds()
            placement_bound = placement.deadline_seconds < run_left
            limit = min(limit, placement.deadline_seconds)
        adapter = model.adapter
        self.generating[call_id] = adapter
        provider_cancel_timer: asyncio.TimerHandle | None = None

        def check_live() -> None:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            if self.o.clock.monotonic() - started >= limit:
                raise TimeoutError
            self._publication_ready(on_external)

        try:
            async with asyncio.timeout(max(limit, 0.001)) as timeout:
                if placement.deadline_seconds is not None:
                    deadline = timeout.when()
                    assert deadline is not None
                    provider_cancel_timer = asyncio.get_running_loop().call_at(
                        deadline, self._cancel_call, call_id
                    )
                stream = adapter.generate(request)
                async with (
                    contextlib.aclosing(stream)
                    if gate is not None and callable(getattr(stream, "aclose", None))
                    else contextlib.nullcontext()
                ):
                    async for event in stream:
                        if gate is not None:
                            check_live()
                        if first_token is None and (
                            isinstance(event, FirstToken | ToolCall)
                            or (isinstance(event, TextDelta) and event.text)
                        ):
                            first_token = self.o.clock.monotonic()
                        if isinstance(event, ToolCall):
                            generation.calls.append(event)
                            if gate is not None:
                                failure = GenerationFailed(
                                    code="invalid_output",
                                    message="answer generation proposed a tool call",
                                )
                                self._cancel_call(call_id)
                                break
                        elif isinstance(event, TextDelta):
                            generation.pieces.append(event.text)
                            if gate is not None:
                                try:
                                    gating = True
                                    for piece in gate.push(event.text):
                                        self._publish_live(
                                            generation,
                                            piece,
                                            model,
                                            check_live,
                                            dispatch_role=dispatch_role,
                                        )
                                    gating = False
                                except StatementRejected:
                                    gating = False
                                    generation.rejected = True
                                    failure = GenerationFailed(
                                        code="interrupted", message="statement rejected"
                                    )
                                    self._cancel_call(call_id)
                                    break
                        elif isinstance(event, Usage):
                            usage = event
                        elif isinstance(event, Completed):
                            finish = event.finish_reason
                            completed = True
                        elif isinstance(event, GenerationFailed):
                            failure = event
                            if gate is not None:
                                self._cancel_call(call_id)
                                break
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                generation, finish = _Generation(), None
                raise asyncio.CancelledError
            if timeout.expired() or (
                placement.deadline_seconds is not None
                and self.o.clock.monotonic() - started >= limit
            ):
                raise TimeoutError
            if gate is not None:
                check_live()
                if not completed and failure is None:
                    failure = GenerationFailed(
                        code="incomplete_output", message="answer generation did not complete"
                    )
                if failure is None and not generation.rejected:
                    gating = True
                    try:
                        tail = gate.finish()
                    except StatementRejected:
                        check_live()
                        generation.rejected = True
                    else:
                        check_live()
                        self._publish_live(
                            generation,
                            self.scrub(tail) if gate.hold else tail,
                            model,
                            check_live,
                            dispatch_role=dispatch_role,
                        )
                    gating = False
        except TimeoutError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                failure = GenerationFailed(code="cancelled", message="model generation interrupted")
                raise asyncio.CancelledError from None
            deadline_missed = placement.deadline_seconds is not None
            if placement_bound:
                self.pending_placement = placement
                failure = GenerationFailed(
                    code="deadline_exceeded", message="placement.deadline spent"
                )
            else:
                exhausted = self.meter.time_exhausted(on_external)
                if placement.deadline_seconds is not None:
                    exhausted = BudgetExceededError(
                        "run_active_seconds", self.o.budgets.run_active_seconds
                    )
                failure = GenerationFailed(code="time_budget", message=f"{exhausted.budget} spent")
            self._cancel_call(call_id)
        except BudgetExceededError as exc:
            exhausted = exc
            failure = GenerationFailed(code="time_budget", message=f"{exc.budget} spent")
            self._cancel_call(call_id)
        except Exception as exc:
            failure = GenerationFailed(code="interrupted", message="model generation interrupted")
            if gate is None or gating or isinstance(exc, _PublicationFailure):
                raise
        except BaseException as exc:
            failure = GenerationFailed(
                code="cancelled" if isinstance(exc, asyncio.CancelledError) else "interrupted",
                message="model generation interrupted",
            )
            raise
        finally:
            if provider_cancel_timer is not None:
                provider_cancel_timer.cancel()
            self.generating.pop(call_id, None)
            duration_ms = round((self.o.clock.monotonic() - started) * 1000)
            if self.workflow is not None and branch is not None:
                self._measure_workflow_call(self.workflow, branch, call_id, duration_ms, failure)
            self._finish_model_call(
                actor,
                {
                    "call_id": call_id,
                    "model": model_id,
                    "purpose": purpose,
                    **label,
                    "duration_ms": duration_ms,
                    "predicted_ms": placement.predicted_ms,
                    "deadline_ms": placement.deadline_ms,
                    "deadline_missed": deadline_missed,
                    "first_token_ms": None
                    if first_token is None
                    else round((first_token - started) * 1000),
                    "finish_reason": finish,
                    "tool_calls": len(generation.calls),
                    "proposed_calls": [
                        {"tool_name": c.tool_name, "arguments": self.redact_arguments(c.arguments)}
                        for c in generation.calls
                    ],
                    "text": self.scrub(generation.text) or None,
                    "usage": None
                    if usage is None
                    else {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cached_tokens": usage.cached_tokens,
                        "reasoning_tokens": usage.reasoning_tokens,
                    },
                    "error": None
                    if failure is None
                    else {"code": failure.code, "message": self.scrub(failure.message)},
                },
            )
        if exhausted is not None:
            raise exhausted
        if deadline_missed:
            raise Escalation(
                "local_slow",
                "local generation missed placement deadline",
                budget="placement.deadline",
            )
        if gate is not None and generation.rejected:
            if generation.published:
                raise _LivePredicateFailure
            return call_id, generation
        if failure is not None:
            if model is not self.o.model:
                raise ExternalFailure(
                    f"external generation failed: {failure.code}", generation_error=failure.code
                )
            if gate is not None and generation.calls and not generation.published:
                return call_id, generation
            raise self._local_unavailable(f"local generation failed: {failure.code}")
        self.meter.check_time(on_external)
        return call_id, generation

    def _finish_model_call(self, actor: str, payload: Mapping[str, Any]) -> None:
        """Keep the sample and completion event in one transaction (RM17, RM27)."""
        usage = payload["usage"]
        if usage is not None:
            usage = {
                **usage,
                "cached_tokens": observed_cached_tokens(
                    usage["input_tokens"], usage["cached_tokens"]
                ),
            }
            payload = {**payload, "usage": usage}
        sample = ModelTiming(
            model=payload["model"],
            actor=actor,
            purpose=payload["purpose"],
            kind="run",
            input_tokens=None if usage is None else usage["input_tokens"],
            cached_tokens=None if usage is None else usage["cached_tokens"],
            output_tokens=None if usage is None else usage["output_tokens"],
            first_token_ms=payload["first_token_ms"],
            duration_ms=payload["duration_ms"],
            succeeded=payload["error"] is None,
            run_id=self.run_id,
            recorded_at=self.o.store.clock.utc_now(),
        )
        with self.o.store.transaction():
            self.o.store.add_model_timing(sample)
            self.emit(EventType.MODEL_FINISHED, actor, payload)

    @staticmethod
    def _measure_workflow_call(
        workflow: _Workflow,
        branch: str,
        call_id: str,
        duration_ms: int,
        failure: GenerationFailed | None,
    ) -> None:
        """Record a workflow call for the terminal summary (RM15, RM25, section 8)."""
        if branch == SYNTHESIS:
            workflow.synthesis_ms.append(duration_ms)
        elif failure is not None and failure.code == "cancelled":
            workflow.abandoned.append(call_id)

    def _answer_options(
        self, defaults: GenerationOptions, *, tools_offered: bool = False
    ) -> GenerationOptions:
        """The caller's options laid over an answer generation's defaults."""
        requested = {
            name: value for name, value in asdict(self.final_options).items() if value is not None
        }
        if tools_offered:
            requested.pop("max_output_tokens", None)
        return replace(defaults, **requested)
