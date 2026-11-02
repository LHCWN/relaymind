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
