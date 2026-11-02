# Automated host experiment: execution record

RM16/RM40/RM44; AC31/AC36; specification 12.6.7. The original inputs and
implementation were preregistered at `8f796b1` before the first paid request.
The complete experiment and its final reviews remain pending.

The [source correction](artifact-writer-correction.json) records an evidence
writer failure after the first native row. Original captures survived.
[Offline recovery](recover-first-row.py) conserves its failed outcome and all
four attempts without a model request. The full 250,000-token reservation
remains charged; 24,438 tokens were reported and two usages remain unknown.
The restricted recovery capture is retained with the run and bound by its
digest; it is a later reconstruction, never a fabricated original log.

The correction changes artifact storage bounds and final-record validation,
not the frozen experimental conditions or inference limits. It is committed
before any subsequent dispatch. Both execution source revisions must be
reported, and the original failed row must never be retried or hidden.

After 14 recorded rows, execution stopped at a row boundary to investigate
the native provider's output-limit parameter. A retained live diagnostic
sent `max_completion_tokens: 1` and received 45 completion tokens; a separate
diagnostic using `max_tokens: 1` received one. Both diagnostics are charged
against the same 13,000,000-token execution cap (82 reported tokens total).
Z.ai's [API reference](https://docs.z.ai/api-reference/llm/chat-completion)
documents `max_tokens`. The automated transport now normalizes the native
parameter for the exact `api.z.ai` host before computing its reservation.
Other endpoints keep their existing parameter handling.

The earlier native calls did not have a verified 8,192-token output bound.
Their unknown usage must therefore be conservatively accounted for at the
provider's documented maximum of 131,072 output tokens per call. The original
captures, row outcomes, journal, and ledger entries remain preserved; a
separate adjustment accounts for the additional uncertainty. This is a
correction to the intended output bound, not a new model, prompt, reasoning
setting, or larger execution budget. Later execution must identify the
corrected source revision, and final evaluation must disclose this limitation.
