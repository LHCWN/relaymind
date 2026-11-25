"""Routing, selector chain, and routing cache methods for orchestrator runs (RM28; #296)."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from relaymind.adapters import (
    GenerationOptions,
    Message,
    Role,
    TokenCountError,
    ToolSchema,
    UnsupportedCapabilityError,
)
from relaymind.orchestrator._run_types import (
    GATEWAY_ACTOR,
    RESUME_MECHANISM,
    ROUTING_MECHANISM,
    Escalation,
    _Generation,
)
from relaymind.orchestrator.placement import PlacementDeclined, marker
from relaymind.orchestrator.route_decision import (
    ExecutionMode,
    Language,
    Route,
    RouteDecisionError,
)
from relaymind.orchestrator.router import Routing, request_language
from relaymind.orchestrator.routing_cache import (
    CacheContext,
    CachedRoute,
    is_eligible,
    key_and_context,
    routing_prompt_version,
)
from relaymind.selection import (
    EXHAUSTED_MECHANISM,
    MODEL_ROUTER,
    Admission,
    ChainResult,
    RoutingGeneration,
    SelectionContext,
    SelectionResult,
    admit,
    catalog_entries,
    parse_selection_request,
)
from relaymind.store.trace_event import EventType


class _SelectionMixin:
    """Routing, selector-chain admission, and routing-cache operations mixed into _Run."""

    def _selector_payload(
        self, kind: EventType | str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Inject pending evidence, then scrub with the secrets known at persistence (RM12).

        A route.selected that names its own selector keeps it; one the gateway writes
        after an interrupted chain, degraded_mode, carries the pending summary (14.3).
        """
        if (
            kind
            in (
                EventType.ROUTE_SELECTED,
                EventType.HANDOFF_STARTED,
                EventType.RUN_FAILED,
                EventType.RUN_CANCELLED,
            )
            and self.pending_selector is not None
        ):
            payload = {"selector": self.pending_selector, **payload}
        if "selector" in payload:
            payload = {
                **payload,
                "selector": self.o.redactor.redact_with_repeats(payload["selector"], self.secrets),
            }
        return payload

    def _selector_persisted(self, kind: EventType | str) -> None:
        """Only a successful write consumes a phase's pending selector evidence (RM17/RM28)."""
        if kind in (
            EventType.ROUTE_SELECTED,
            EventType.HANDOFF_STARTED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
        ):
            self.pending_selector = None

    def _resume_route(self, skill_id: str) -> Routing:
        """The clarify named a skill: check the new input, no generation (section 5)."""
        assert self.language is not None
        try:
            routing = self.o.router.skill_routing(
                skill_id,
                self.messages,
                mode=self.mode,
                language=self.language,
                no_local=self.no_local,
            )
        except RouteDecisionError as exc:
            raise Escalation("invalid_output", f"resume decision rejected: {exc}") from exc
        self.routing_placement = marker()
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {
                "decision": routing.decision.to_json_object(),
                "mechanism": RESUME_MECHANISM,
                **self._cache_label("miss"),
                **self._local_label(),
            },
        )
        return routing

    async def _route(self) -> Routing:
        self.routing_placement = marker()
        router = self.o.router
        if self.mode is ExecutionMode.EXTERNAL_ONLY:
            decision = router.execution_mode_decision(self.messages)
            self.emit(
                EventType.ROUTE_SELECTED,
                GATEWAY_ACTOR,
                {
                    "decision": decision.to_json_object(),
                    "mechanism": "execution_mode",
                    **self._cache_label("miss"),
                },
            )
            return Routing(decision)
        if self.no_local:
            # RM26: managed external execution; no local generation, cache, or bypass.
            return await self._select(None, None)

        assert self.o.model is not None
        adapter = self.o.model.adapter
        try:
            capabilities = await adapter.capabilities()
            capabilities.require("tool_calling", "tokenizer_available")
        except UnsupportedCapabilityError as exc:
            raise self._local_unavailable(str(exc)) from exc

        # One key per routing request: the lookup and the store share it.
        cache_key = self._cache_key() if self.o.routing_cache is not None else None
        cached = self._cached_routing(cache_key)
        if cached is not None:
            return cached

        routing_messages = router.routing_messages(self.messages)
        functions = router.functions()
        try:
            input_tokens = await adapter.count_tokens(routing_messages, functions)
        except (TokenCountError, UnsupportedCapabilityError) as exc:
            raise self._local_unavailable(f"router input could not be counted: {exc}") from exc

        self._routing_count = (tuple(routing_messages), tuple(functions), input_tokens)
        if not router.within_input_cap(input_tokens):
            decision = router.bypass_decision(self.messages, self.mode, language=self.language)
            self.emit(
                EventType.ROUTE_SELECTED,
                GATEWAY_ACTOR,
                {
                    "decision": decision.to_json_object(),
                    "mechanism": "bypass",
                    "input_tokens": input_tokens,
                    "input_cap": self.o.budgets.routing_input_tokens,
                    **self._cache_label("miss"),
                },
            )
            return Routing(decision)

        return await self._select(input_tokens, cache_key)

    async def _select(
        self, input_tokens: int | None, cache_key: tuple[str, CacheContext] | None
    ) -> Routing:
        """Run the selector chain and record the admitted choice, or the chain's failure."""
        generations: list[tuple[str, _Generation]] = []
        generation_stop: Exception | asyncio.CancelledError | None = None

        async def generate(
            messages: Sequence[Message], functions: Sequence[ToolSchema], options: GenerationOptions
        ) -> RoutingGeneration:
            nonlocal generation_stop
            if generation_stop is not None:
                raise generation_stop
            # The generation owns tokenizer, stage/run and placement deadlines,
            # including provider cancellation. An enclosing timer must not turn
            # its expiry into apparent user cancellation. Keep the gateway's
            # original deadline for subsequent non-model selector work.
            gateway_deadline = timeout.when()
            timeout.reschedule(None)
            try:
                call_id, generation = await self._generate("routing", messages, functions, options)
            except PlacementDeclined:
                # A genuine pre-dispatch decline continues the configured chain.
                timeout.reschedule(gateway_deadline)
                raise
            except (Exception, asyncio.CancelledError) as stopped:
                generation_stop = stopped
                # A selector may catch the guarded callback stop. Interrupt any
                # further await immediately, then preserve this original stop
                # instead of replacing it with an enclosing timeout's budget.
                timeout.reschedule(asyncio.get_running_loop().time())
                raise
            else:
                timeout.reschedule(gateway_deadline)
            generations.append((call_id, generation))
            return RoutingGeneration(
                calls=tuple(generation.calls), model=self.active_model.model_id, call_id=call_id
            )

        def admit_result(result: SelectionResult) -> Admission:
            return admit(
                result,
                router=self.o.router,
                messages=self.messages,
                mode=self.mode,
                language=self.language,
                no_local=self.no_local,
            )

        selection_chain = self.o.no_local_chain if self.no_local else self.o.selection_chain

        def progress(result: Any) -> None:
            self.pending_selector = self.o.redactor.redact_with_repeats(
                result.selector_record(), self.secrets
            )

        limit = self.meter.remaining_seconds()
        if limit <= 0:
            raise self.meter.time_exhausted()
        timeout = asyncio.timeout(limit)
        try:
            async with timeout:
                chain = await selection_chain.run(
                    self._selection_request(),
                    SelectionContext(self.messages, generate),
                    admit_result,
                    on_progress=progress,
                    remaining_seconds=self.meter.remaining_seconds,
                    time_exhausted=self.meter.time_exhausted,
                )
        except TimeoutError:
            if timeout.expired():
                # Preserve an authoritative callback stop when the guard had to
                # interrupt a selector that caught it; otherwise this guard owns
                # the gateway time budget, not an ordinary selector timeout.
                if self.pending_selector:
                    self.pending_selector["attempts"][-1]["outcome"] = (
                        "cancelled"
                        if isinstance(generation_stop, asyncio.CancelledError)
                        else "error"
                    )
                if generation_stop is not None:
                    raise generation_stop from None
                raise self.meter.time_exhausted() from None
            raise

        # Every attempt of a chain that returned is complete and keeps its outcome
        # (spec 14.3): a budget spent meanwhile stops the run after route.selected.
        # A rejected routing output has no route.selected, so the budget stop keeps its
        # priority over invalid_output and the pending summary goes on the terminal event.
        spent = self.meter.remaining_seconds() <= 0
        if spent and chain.admitted is None and chain.rejected_model_output() is not None:
            raise self.meter.time_exhausted()
        routing = self._record_selection(chain, generations, input_tokens, cache_key)
        if spent:
            raise self.meter.time_exhausted()
        return routing

    def _record_selection(
        self,
        chain: ChainResult[Admission],
        generations: Sequence[tuple[str, _Generation]],
        input_tokens: int | None,
        cache_key: tuple[str, CacheContext] | None,
    ) -> Routing:
        """Write route.selected for a chain that returned (RM28, section 8)."""
        if chain.admitted is None or chain.selector is None:
            rejected = chain.rejected_model_output()
            if rejected is not None:
                # RM04: the model router's output was rejected and nothing else chose.
                raise Escalation("invalid_output", f"routing output rejected: {rejected.detail}")
            decision = self.o.router.selection_failed_decision(
                self.messages, self.mode, language=self.language
            )
            if any(a.outcome.value == "not_placed" for a in chain.attempts):
                decision = replace(decision, reason_code="local_slow")
            self.emit(
                EventType.ROUTE_SELECTED,
                GATEWAY_ACTOR,
                {
                    "decision": decision.to_json_object(),
                    "mechanism": EXHAUSTED_MECHANISM,
                    **self._cache_label("miss"),
                    "selector": chain.selector_record(),
                    **self._local_label(),
                },
            )
            self.narrowed = None
            return Routing(decision)
        admission = chain.admitted
        routing = admission.routing
        self.narrowed = admission.tools
        narrowed = {} if admission.tools is None else {"tools_narrowed": list(admission.tools)}
        if chain.selector.name == MODEL_ROUTER and generations:
            call_id, generation = generations[-1]
            self.emit(
                EventType.ROUTE_SELECTED,
                self.actor,
                {
                    "decision": routing.decision.to_json_object(),
                    "mechanism": ROUTING_MECHANISM,
                    "model": self.active_model.model_id,
                    "call_id": call_id,
                    "input_tokens": input_tokens,
                    "model_language": generation.calls[0].arguments.get("language"),
                    **self._cache_label("miss"),
                    "selector": chain.selector_record(),
                    **narrowed,
                },
            )
            self._store_route(routing, cache_key)
            return routing
        assert chain.result is not None
        if self.routing_placement.role != "none":
            self.routing_placement = marker()
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {
                "decision": routing.decision.to_json_object(),
                "mechanism": chain.result.mechanism,
                **self._cache_label("miss"),
                "selector": chain.selector_record(),
                **narrowed,
                **self._local_label(),
            },
        )
        return routing

    def _selection_request(self) -> Any:
        """The SelectionRequest of this routing, without a deadline (RM28, section 8)."""
        roles = (Role.SYSTEM, Role.DEVELOPER, Role.USER, Role.ASSISTANT)
        conversation = [
            {"role": m.role.value, "content": m.content}
            for m in self.messages
            if m.role in roles and m.content is not None
        ]
        return parse_selection_request(
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "conversation": self.o.redactor.redact_with_repeats(conversation, self.secrets),
                "language": self._routing_language().value,
                "execution_mode": self.mode.value,
                "catalog": catalog_entries(self.o.skills, self.o.router.offered),
                "permissions": sorted(self.o.caller.permissions),
                "deadline_ms": None,
            }
        )

    def _cache_label(self, outcome: str) -> dict[str, str]:
        """Spec 0.26 Labels: every route.selected of a cache run says hit or miss."""
        return {} if self.o.routing_cache is None else {"routing_cache": outcome}

    def _cache_key(self) -> tuple[str, CacheContext]:
        o = self.o
        return key_and_context(
            messages=self.messages,
            language=self._routing_language().value,
            mode=self.mode.value,
            offered=[o.skills.get(name) for name in o.router.offered],
            tools=o.tools,
            policy=o.policy,
            caller=o.caller,
            model_id=self.active_model.model_id,
            enabled_workflows=o.enabled_workflows,
            routing_prompt_version=routing_prompt_version(o.router.functions()),
            selector_chain=o.selection_chain.identity,
        )

    def _routing_language(self) -> Language:
        return self.language or request_language(self.messages)

    def _cached_routing(self, cache_key: tuple[str, CacheContext] | None) -> Routing | None:
        """The routing of a cache hit, or None for a miss (no routing generation then ran)."""
        cache = self.o.routing_cache
        if cache is None or cache_key is None:
            return None
        key, context = cache_key
        hit = cache.lookup(key, context)
        if hit is None:
            return None
        try:
            routing = self.o.router.skill_routing(
                hit.route.skill_id,
                self.messages,
                mode=self.mode,
                language=self._routing_language(),
            )
        except RouteDecisionError:
            routing = None
        if routing is None or routing.decision.route is not Route.LOCAL:
            cache.drop(key)
            return None
        self.emit(
            EventType.CACHE_HIT,
            GATEWAY_ACTOR,
            {
                "originating_version": hit.route.skill_version,
                "originating_run_id": hit.route.run_id,
                "skill": {
                    "name": hit.route.skill_id,
                    "version": hit.route.skill_version,
                    "content_hash": hit.route.skill_hash,
                },
                "age_ms": hit.age_ms,
                "key": key,
            },
        )
        self.emit(
            EventType.ROUTE_SELECTED,
            GATEWAY_ACTOR,
            {
                "decision": routing.decision.to_json_object(),
                "mechanism": "routing_cache",
                "routing_cache": "hit",
            },
        )
        return routing

    def _store_route(self, routing: Routing, cache_key: tuple[str, CacheContext] | None) -> None:
        """Keep the skill choice of a local read-only route (spec 0.26: eligibility)."""
        cache = self.o.routing_cache
        decision = routing.decision
        if (
            cache is None
            or cache_key is None
            or decision.route is not Route.LOCAL
            or decision.skill_id is None
        ):
            return
        skill = self.o.skills.get(decision.skill_id)
        if not is_eligible(skill, self.o.tools):
            return
        key, context = cache_key
        cache.store(
            key,
            context,
            CachedRoute(
                skill_id=skill.name,
                skill_version=skill.version,
                skill_hash=skill.content_hash,
                run_id=self.run_id,
            ),
        )
