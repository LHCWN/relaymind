"""Bound record files and spec Time values shared by record verifiers (RM16; spec 12.6.6)."""

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from relaymind.evaluation.asset_corpus_io import read_local
from relaymind.evaluation.asset_whole_task import WholeTaskRejected

_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")


def record_time(value: str) -> tuple[datetime, Decimal]:
    """A spec Time (UTC RFC 3339 with Z) as a comparable instant.

    The schema checks only the shape; an impossible date or clock value
    (month 13, 30 February, hour 24, minute 60 or a leap second) rejects with
    the closed reason ``invalid_record_time``. Fractional seconds of any
    length are kept exactly.
    """
    match = _TIME.fullmatch(value)
    try:
        if match is None:
            raise ValueError(value)
        *parts, fraction = match.groups()
        instant = datetime(*(int(part) for part in parts), tzinfo=UTC)
    except ValueError:
        raise WholeTaskRejected("invalid_record_time") from None
    return instant, Decimal("0." + (fraction or "0"))


def read_bound(root: Path, ref: Mapping, limit: int) -> bytes:
    """The bytes of one bound file, checked against its size and SHA-256."""
    try:
        payload = read_local(root, ref["path"], limit)
    except (OSError, ValueError) as exc:
        raise WholeTaskRejected("record_subject_unavailable") from exc
    if len(payload) != ref["bytes"] or hashlib.sha256(payload).hexdigest() != ref["sha256"]:
        raise WholeTaskRejected("record_digest_mismatch")
    return payload
