"""Propose the few assets one prompt needs from the list the host really has (RM45, section 7.7).

A proposal only: nothing here executes, grants or removes anything. Nothing is retained except a
bounded in-memory token cache of asset text. Each stage is bounded by one total time, and a failed
stage degrades the answer and never fails it.
"""

import asyncio
import functools
import hashlib
import json
import math
import re
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from itertools import islice, zip_longest

from relaymind.assets.catalog import MAX_OFFER_RECORDS
from relaymind.canonical import canonical_digest
from relaymind.recommendations.limits import MAX_TASK_BYTES
from relaymind.redaction import REDACTED
from relaymind.schema_util import StrictValidator, first_schema_error, load_packaged_schema
from relaymind.selection.asset_admission import validate_against_offer
from relaymind.selection.asset_contract import (
    SelectorContext,
    SelectorIdentity,
    parse_asset_request,
    parse_asset_result,
)
from relaymind.selection.asset_lexical import tokenize
from relaymind.selection.http import validate_url

MAX_GUIDANCE_BYTES = 32 * 1024 * 1024
MAX_NAME_BYTES = 128
MAX_DESCRIPTION_BYTES = 2048
# Three times the most assets one request may hold; a digest key keeps no request text alive.
_TOKEN_CACHE_LIMIT = 60_000
_SHOWN_LEAD_CHARS = 100  # What an asset is about.
_SHOWN_DESCRIPTION_CHARS = 320  # Room for what sets it apart from its neighbors in the shortlist.
_JOIN = " ... "
_SUMMARY_LEAD_CHARS = 60
_SUMMARY_CHARS = 200
_LOCAL_KEYWORD_TOKENS = 80  # The local model runs with thinking off.
# An external model may think first, and thinking counts against its output limit.
_EXTERNAL_ANSWER_TOKENS = 300
# Tokenizing a list the first time takes about 0.3 ms an asset, too long for the event loop.
_THREAD_ABOVE = 200
# A host tool's own schema is not offered, so every projection carries the same open one.
_ANY_INPUT = {"type": "object"}
_ANY_INPUT_HASH = canonical_digest(_ANY_INPUT)
_MAX_TASK_PARTS = 8
# A Latin mark ends a sentence only before a space, so 3.14, github.com and ... stay whole.
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|(?<=[。！？；\n])\s*")
# Chinese words that carry no task of their own, longest first.
_FILLER_PHRASES = tuple(
    sorted(
        "好的 好吧 好 可以 行 对 嗯 是的 是 没问题 就这么做 这么做 这样做 就这样 这样 "
        "就用 用 这个 那个 就 继续 开始 吧 了 啊 呢 请 麻烦 你 帮我 帮 我 做 谢谢 多谢 "
        "一下 吗".split(),
        key=len,
        reverse=True,
    )
)
# Words that carry no task of their own. A short prompt of only these is a follow-up in any list.
_FILLER_WORDS = frozenset(
    "a again ahead all and are as at be but can could do does done for go got if in is it its "
    "just let me my now of off ok okay on one or please same so sure that the them then these "
    "this those thanks thank to too up us we what with yeah yes you".split()
)
_MIN_TASK_WORDS = 3  # A shorter piece is not a task of its own, such as "Fix it."
# A prompt this short may depend on the conversation, such as "yes, do that".
_FOLLOW_UP_WORDS = 6
_FALLBACK_RESERVE_S = 1.0  # At most a quarter of the total time.
# Without a picker, in a list this long, only strong matches are named (docs/EVIDENCE.md).
_STRONG_ABOVE = 50
_COMMON_SHARE = 0.1  # A word in more of the list than this tells nothing.
_STRONG_FLOOR = 1.3  # The best match must score this many times the rarest single word.
_STRONG_SHARE = 0.5  # Others must score at least this share of the best.

_REQUEST = StrictValidator(load_packaged_schema("relaymind.guidance", "guidance_request.v1.json"))
_RESULT = StrictValidator(load_packaged_schema("relaymind.guidance", "guidance_result.v1.json"))

KEYWORD_PROMPT = (
    "Rewrite the user's request as search keywords for finding skills and tools. The request can "
    "be in any language. Write one line for each separate task in the request. On each line write "
    "15 to 25 English keywords: the action, the object, the product names, and common synonyms. "
    "Keep any names from the request. Output only the lines of keywords, separated by spaces."
)
# About three times faster on the small local model, with similar results (docs/EVIDENCE.md).
LOCAL_KEYWORD_PROMPT = (
    "Translate the user's request into English. Then list English words that a tool for this job "
    "would mention in its description, with synonyms. Output only plain English words separated "
    "by spaces."
)
PICKER_PROMPT = (
    "You choose which skills and tools an AI assistant needs for a user's request. Understand the "
    "intent. The request can be in any language. You get a numbered list. Each item shows what it "
    "does and the parts that set it apart. Many items can look alike: compare what makes them "
    "different, such as platform, vendor, version, region, format or method, with the clues in the "
    "request, including indirect ones. Reply with JSON only: "
    '{"picks":[numbers],"unclear":false}, best first.\n'
    "- Find each separate task in the request, and pick the best item for each separate task. A "
    "simple request has one task and needs one item. A big workflow has several tasks and needs "
    "several items, usually 3 to 5 and sometimes more.\n"
    "- Do not add an item that no task needs, and do not pick two alike items for one task.\n"
    "- If a task fits several alike items and nothing in the request decides between them, set "
    '"unclear":true and pick all of those, at most %d in all.\n'
    "- Pick an item about finding or managing skills only when the request asks for that.\n"
    '- If nothing fits, reply {"picks":[],"unclear":false}.'
)

ChatFn = Callable[[list[dict], int], Awaitable[tuple[str, Mapping | None]]]


class GuidanceError(ValueError):
    """The request is not a valid GuidanceRequestV1."""


@dataclass(frozen=True)
class GuidanceSettings:
    enabled: bool = False
    keywords: str = "none"
    picker: str = "none"
    primary: str = "builtin"
    name: str | None = None
    version: str | None = None
    url: str | None = None
    fallback: bool = True
    total_ms: int = 12000
    shortlist: int = 40
    max_picks: int = 8
    unpicked_limit: int = 8
    max_choices: int = 6


GUIDANCE_FIELDS = frozenset(f.name for f in fields(GuidanceSettings))


def parse_guidance_settings(value: Mapping) -> GuidanceSettings:
    """Closed checks at startup. No model or selector connection is made (section 14.5)."""
    unknown = set(value) - GUIDANCE_FIELDS
    if unknown:
        raise ValueError(f"guidance has unknown keys: {', '.join(sorted(unknown))}")
    raw = {**vars(GuidanceSettings()), **value}
    for key in ("enabled", "fallback"):
        if type(raw[key]) is not bool:
            raise ValueError(f"guidance {key} must be boolean")
    choices = dict(
        keywords=("none", "local", "external"),
        picker=("none", "external"),
        primary=("builtin", "http"),
    )
    for key, allowed in choices.items():
        if raw[key] not in allowed:
            raise ValueError(f"guidance {key} must be one of {', '.join(allowed)}")
    limits = dict(
        total_ms=(1, None),
        shortlist=(1, None),
        max_picks=(1, 20),
        unpicked_limit=(1, 20),
        max_choices=(1, 20),
    )
    for key, (low, high) in limits.items():
        item = raw[key]
        if type(item) is not int or item < low or (high is not None and item > high):
            raise ValueError(f"guidance {key} is out of range")
    if raw["primary"] == "http":
        for key in ("name", "version", "url"):
            if type(raw[key]) is not str or not raw[key].strip():
                raise ValueError("guidance http requires name, version and url as text")
        validate_url(raw["url"])
    return GuidanceSettings(**raw)


def parse_guidance_request(value: object) -> dict:
    """A closed GuidanceRequestV1 with its byte limits, or GuidanceError."""
    if first_schema_error(_REQUEST, value) is not None:
        raise GuidanceError("invalid_request")
    if not value["task_text"].strip() or len(value["task_text"].encode("utf-8")) > MAX_TASK_BYTES:
        raise GuidanceError("invalid_request")
    if len(value.get("recent_text", "").encode("utf-8")) > MAX_TASK_BYTES:
        raise GuidanceError("invalid_request")
    seen = set()
    for item in value["assets"]:
        key = (item["kind"], item["name"])
        if (
            key in seen
            or len(item["name"].encode("utf-8")) > MAX_NAME_BYTES
            or len(item["description"].encode("utf-8")) > MAX_DESCRIPTION_BYTES
        ):
            raise GuidanceError("invalid_request")
        seen.add(key)
    return value


def validate_guidance_result(value: object) -> dict:
    error = first_schema_error(_RESULT, value)
    if error is not None:
        raise ValueError(f"invalid guidance result: {error.message}")
    return value


def _field_name(name: str) -> str:
    """A field name as people write it: api_key, api-key, API KEY or apiKey."""
    return r"[-_ ]?".join(re.escape(part) for part in re.split(r"[-_ ]+", name) if part)


@functools.cache
def _secret_pattern(fields: frozenset[str]) -> re.Pattern | None:
    names = "|".join(_field_name(name) for name in sorted(fields, key=len, reverse=True))
    if not names:
        return None
    # The name may be quoted, as in JSON, or carry a prefix, as in OPENAI_API_KEY.
    return re.compile(
        rf"(?i)(?<![a-z0-9_-])([\"']?(?:[a-z0-9]+[_-])*(?:{names})[\"']?)(\s*[:=：＝]\s*)"
        # A value, after any scheme word, ends at a space or at punctuation, ASCII or fullwidth,
        # because Chinese text goes on without a space.
        r"((?:(?:bearer|basic|token)\s+)?"
        r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;)}\]）】”’，。；！？：、]+))"
    )


def clip_bytes(text: str, limit: int) -> str:
    """The longest prefix within the UTF-8 byte limit that does not split a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def clip_end(text: str, limit: int) -> str:
    """The longest suffix within the UTF-8 byte limit that does not split a character."""
    return text.encode("utf-8")[-limit:].decode("utf-8", "ignore") if limit else ""


def redact_prompt(redactor, text: str) -> str:
    """Prompt text with `secret_field: value` and `secret_field=value` values replaced (RM12).

    Redactor.redact works on JSON keys, and a prompt is free text. Words that only name a field
    stay, so a question about API keys still reads the same.
    """
    pattern = _secret_pattern(frozenset(redactor.secret_fields))
    return text if pattern is None else pattern.sub(rf"\1\2{REDACTED}", text)


def needs_keywords(text: str) -> bool:
    """True when the prompt has letters outside the Latin script (section 7.7, stage 2)."""
    return any(
        unicodedata.category(c).startswith("L") and not unicodedata.name(c, "").startswith("LATIN")
        for c in text
    )


_token_cache: dict[bytes, frozenset[str]] = {}


def _tokens(item: Mapping) -> frozenset[str]:
    key = hashlib.blake2b(
        f"{item['name']}\0{item['description']}".encode("utf-8", "surrogatepass"), digest_size=16
    ).digest()
    found = _token_cache.get(key)
    if found is None:
        if len(_token_cache) >= _TOKEN_CACHE_LIMIT:
            _token_cache.clear()
        found = tokenize(f"{item['name']} {item['description']}")
        _token_cache[key] = found
    return found


@dataclass(frozen=True)
class _Ranked:
    """Every match, best first, and the few strong enough to name without a model's choice."""

    items: list[Mapping]
    strong: list[Mapping]


def _rank_now(assets: Sequence[Mapping], query: frozenset[str], tokens=None) -> _Ranked:
    """Rarity-weighted overlap: a word few assets have counts for more than a common word.

    `tokens`, the assets' tokens in order, saves a cache lookup per asset when several rankings
    run over the same list."""
    tokens = tokens if tokens is not None else [_tokens(item) for item in assets]
    overlaps = [(i, query & found) for i, found in enumerate(tokens)]
    frequency: dict[str, int] = {}
    for _, words in overlaps:
        for word in words:
            frequency[word] = frequency.get(word, 0) + 1
    total = len(assets)
    weight = {word: math.log(1 + total / count) for word, count in frequency.items()}
    scored = [(-sum(weight[w] for w in words), i) for i, words in overlaps if words]
    scored.sort()
    items = [assets[i] for _, i in scored]
    if total < _STRONG_ABOVE:
        return _Ranked(items, items)  # In a small list rarity says little.
    usable = {word for word, count in frequency.items() if count <= _COMMON_SHARE * total}
    kept = [(i, words & usable) for i, words in overlaps]
    rescored = [(-sum(weight[w] for w in words), i) for i, words in kept if words]
    rescored.sort()
    if not rescored or -rescored[0][0] < _STRONG_FLOOR * math.log(1 + total):
        return _Ranked(items, [])
    bar = _STRONG_SHARE * -rescored[0][0]
    return _Ranked(items, [assets[i] for score, i in rescored if -score >= bar])


def _task_parts(text: str) -> list[str]:
    """The sentences of a prompt that may each be a task, or nothing for a single task."""
    pieces = [part.strip() for part in re.split(_SENTENCE_END, text)]
    parts = [part for part in pieces if part and _word_count(part) >= _MIN_TASK_WORDS]
    if len(parts) < 2:
        return []
    return parts[:_MAX_TASK_PARTS]


def _only_filler(text: str) -> bool:
    """True when a prompt holds no task of its own, such as "yes, do that" or "好的，就这么做"."""
    rest = text.lower()
    for phrase in _FILLER_PHRASES:
        rest = rest.replace(phrase, " ")
    words = re.findall(r"\w+", rest)
    return all(word in _FILLER_WORDS for word in words)


def _interleave(lists: Sequence[Sequence[Mapping]]) -> list[Mapping]:
    """Take from each list in turn, so that one task's many matches cannot crowd out another's."""
    merged, seen = [], set()
    for row in zip_longest(*lists):
        for item in row:
            if item is not None and id(item) not in seen:
                seen.add(id(item))
                merged.append(item)
    return merged


def _word_count(text: str) -> int:
    """Words in text. A run of a script written without spaces counts about two letters a word."""
    count = 0
    for run in re.findall(r"\w+", text):
        spaced = re.sub(r"[぀-ヿ㐀-鿿가-힯]", "", run)
        count += (1 if spaced else 0) + (len(run) - len(spaced) + 1) // 2
    return count


def _named_in_prompt(assets: Sequence[Mapping], text: str) -> list[Mapping]:
    """Assets whose whole name appears in the prompt as its own word. Single common words do not
    count, so a tool called "search" is not pinned by every prompt that says search."""
    lowered = text.lower()
    named = []
    for item in assets:
        name = item["name"].lower()
        if not re.search(r"[-_.:]", name) or name not in lowered:
            continue
        # Latin letters only around the name: Chinese is written without spaces.
        # A separator followed by a letter or digit continues a name, as in name.v2 or name:extra.
        if re.search(rf"(?<![a-z0-9_.:-]){re.escape(name)}(?![a-z0-9_-]|[.:][a-z0-9])", lowered):
            named.append(item)
    return named


async def _rank(assets: Sequence[Mapping], query: frozenset[str], tokens=None) -> _Ranked:
    if len(assets) > _THREAD_ABOVE:
        return await asyncio.to_thread(_rank_now, assets, query, tokens)
    return _rank_now(assets, query, tokens)


def _rank_tasks_now(assets, query: frozenset[str], parts: Sequence[str]) -> _Ranked:
    if not parts:
        return _rank_now(assets, query)
    tokens = [_tokens(item) for item in assets]  # One lookup per asset for every ranking.
    whole = _rank_now(assets, query, tokens)
    ranked = [_rank_now(assets, tokenize(part), tokens) for part in parts]
    return _Ranked(
        _interleave([whole.items, *(r.items for r in ranked)]),
        # Keywords written for the whole prompt match no single task's own words.
        _interleave([r.strong for r in ranked]) or whole.strong,
    )


async def _rank_tasks(assets, query: frozenset[str], parts: Sequence[str]) -> _Ranked:
    """The whole prompt's ranking, with each task's own best matches taken in turn."""
    if len(assets) > _THREAD_ABOVE:
        return await asyncio.to_thread(_rank_tasks_now, assets, query, parts)
    return _rank_tasks_now(assets, query, parts)


class _Run:
    def __init__(self, settings: GuidanceSettings, clock: Callable[[], float], redact):
        self.clock = clock
        self.redact: Callable[[str], str] = redact
        self.deadline = clock() + settings.total_ms / 1000
        self.stages: list[dict] = []
        self.degraded = False

    def remaining(self) -> float:
        return self.deadline - self.clock()

    def record(self, stage, outcome, started=None, usage=None):
        """One entry per stage. A stage that runs again adds its time and usage, and keeps the
        worse outcome, so summed times are right and the stage still says why it degraded."""
        ms = 0.0 if started is None else max(0.0, (self.clock() - started) * 1000)
        entry = dict(stage=stage, outcome=outcome, ms=ms, usage=_usage(usage))
        if outcome in ("failed", "timeout"):
            self.degraded = True
        earlier = next((s for s in self.stages if s["stage"] == stage), None)
        if earlier is None:
            self.stages.append(entry)
        elif earlier["outcome"] == "skipped":
            earlier.update(entry)
        elif outcome != "skipped":
            earlier["ms"] += ms
            # Unknown stays unknown: a sum with an unknown part is not a smaller known number.
            if earlier["usage"] is not None and entry["usage"] is not None:
                earlier["usage"] = {
                    key: None
                    if earlier["usage"][key] is None or entry["usage"][key] is None
                    else earlier["usage"][key] + entry["usage"][key]
                    for key in earlier["usage"]
                }
            else:
                earlier["usage"] = None
            if outcome != "ok":
                earlier["outcome"] = outcome

    async def bounded(
        self, stage: str, work: Callable[[], Awaitable], *, started=None, usage=None, reserve=0.0
    ):
        """(ok, value). A stage past the total time, less `reserve` seconds kept for a later stage,
        or failing, is recorded and never raises."""
        started = self.clock() if started is None else started
        left = self.remaining() - reserve
        if left <= 0:
            self.record(stage, "timeout", started, usage)
            return False, None
        try:
            return True, await asyncio.wait_for(work(), left)
        except TimeoutError:
            self.record(stage, "timeout", started, usage)
        except Exception:  # A cancellation is a BaseException and goes through.
            self.record(stage, "failed", started, usage)
        return False, None


def _usage(usage: Mapping | None) -> dict | None:
    """A provider's counts, with anything but a non-negative integer reported as unknown."""

    def count(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    if not usage:
        return None
    return dict(
        input_tokens=count(usage.get("input_tokens")),
        output_tokens=count(usage.get("output_tokens")),
    )


def _pick(item: Mapping) -> dict:
    return dict(name=item["name"], kind=item["kind"])


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(_SENTENCE_END, text) if part]


def _distinct_text(
    description: str, frequency: Mapping[str, int], total: int, *, limit: int, lead_limit: int
) -> str:
    """The opening, then the sentences with the words its neighbors in the list lack.

    Similar assets often share a long opening and differ late, so a plain cut hides the difference.
    """
    clean = " ".join(description.split())
    if len(clean) <= limit:
        return clean
    parts = _sentences(clean)
    if len(parts) < 2:
        return clean[:limit]
    lead = parts[0][:lead_limit]
    room = limit - len(lead) - len(_JOIN)

    def rarity(sentence: str) -> float:
        found = tokenize(sentence)
        weight = sum(math.log(1 + total / frequency.get(word, 1)) for word in found)
        return weight / max(1, len(found))

    chosen = set()
    for index in sorted(range(1, len(parts)), key=lambda i: (-rarity(parts[i]), i)):
        if len(parts[index]) < room:
            chosen.add(index)
            room -= len(parts[index]) + len(_JOIN)
    return _JOIN.join([lead, *(parts[i] for i in sorted(chosen))])


def _frequency(items: Sequence[Mapping]) -> dict[str, int]:
    frequency: dict[str, int] = {}
    for item in items:
        for word in tokenize(item["description"]):
            frequency[word] = frequency.get(word, 0) + 1
    return frequency


def _listing(short: Sequence[Mapping], redact: Callable[[str], str]) -> str:
    frequency = _frequency(short)
    lines = []
    for number, item in enumerate(short, start=1):
        kind = "tool" if item["kind"] == "mcp_tool" else "skill"
        # Redacted first: a secret split at a sentence end must not lose its field name.
        text = _distinct_text(
            redact(item["description"]),
            frequency,
            len(short),
            limit=_SHOWN_DESCRIPTION_CHARS,
            lead_limit=_SHOWN_LEAD_CHARS,
        )
        lines.append(f"{number}. [{kind}] {item['name']}: {text}")
    return "\n".join(lines)


def _picks(picks: Sequence[Mapping], redact: Callable[[str], str]) -> list[dict]:
    """Exact entries. Several picks also carry what sets them apart, for the host's model."""
    if len(picks) < 2:
        return [_pick(p) for p in picks]
    frequency = _frequency(picks)
    out = []
    for p in picks:
        # Redacted first: a secret split at a sentence end must not lose its field name.
        summary = _distinct_text(
            redact(p["description"]),
            frequency,
            len(picks),
            limit=_SUMMARY_CHARS,
            lead_limit=_SUMMARY_LEAD_CHARS,
        )
        # A description can hold a secret too, and the summary goes into a model's prompt.
        summary = redact(summary)[:_SUMMARY_CHARS]  # [redacted] can be longer than the value.
        # An asset with no description has nothing to set it apart, so it carries no summary.
        out.append(dict(_pick(p), summary=summary) if summary.strip() else _pick(p))
    return out


def _first_object_with(text: str, key: str) -> dict:
    """The first JSON object in text that has key, wherever it starts and whatever follows it."""
    decoder = json.JSONDecoder()
    for start in (match.start() for match in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict) and key in value:
            return value
    raise ValueError(f"no JSON object with {key}")


def _parse_picks(text: str, shortlist: Sequence[Mapping]) -> tuple[list[Mapping], bool]:
    """Only entries of the shortlist, and whether the model said the request does not decide."""
    answer = _first_object_with(text, "picks")
    picks = answer["picks"]
    if not isinstance(picks, list):
        raise ValueError("picks is not a list")
    chosen, seen = [], set()
    for number in picks:
        if type(number) is int and 1 <= number <= len(shortlist) and number not in seen:
            seen.add(number)
            chosen.append(shortlist[number - 1])
    if picks and not chosen:  # Names or numbers off the list: not an answer that nothing fits.
        raise ValueError("the answer names no item of the shortlist")
    return chosen, answer.get("unclear") is True


def _result(run, *, picks, chosen_by, mechanism, offered, shortlist, unclear=False) -> dict:
    decision = "no_match" if not picks else "choose" if unclear and len(picks) > 1 else "picks"
    return validate_guidance_result(
        dict(
            schema_version=1,
            decision=decision,
            picks=_picks(picks, run.redact),
            chosen_by=chosen_by if picks else "none",
            mechanism=mechanism,
            degraded=run.degraded,
            offered=offered,
            shortlist=shortlist,
            stages=run.stages,
        )
    )


_MARKUP_START = re.compile(r"<(?=[A-Za-z!?/])")


def _projected(text: str) -> str:
    """A host description as an AssetRecordV1 accepts it: no control characters, nothing that
    reads as markup, no line more than 3 times. One odd host text must not fail the whole offer."""
    text = unicodedata.normalize("NFC", text)
    text = "".join(c for c in text if c in "\t\r\n" or not (ord(c) < 32 or 127 <= ord(c) <= 159))
    text = _MARKUP_START.sub("< ", text)
    seen: dict[str, int] = {}
    kept = []
    for line in text.splitlines():
        seen[line.strip()] = seen.get(line.strip(), 0) + 1
        if not line.strip() or seen[line.strip()] <= 3:
            kept.append(line)
    return "\n".join(kept).strip()


def _record(item: Mapping, redact: Callable[[str], str]) -> dict:
    """One host entry as an AssetRecordV1 projection for the published selection v2 request."""
    # A hosted selector can sit behind the loopback address, so its text is redacted like a prompt.
    description = _projected(redact(item["description"])) or item["name"]
    if item["kind"] == "skill":
        identity = dict(package_id=item["name"], package_version=None)
    else:
        identity = dict(
            server_id="host",
            tool_name=item["name"],
            exposed_name=item["name"],
            input_schema=_ANY_INPUT,
            schema_hash=_ANY_INPUT_HASH,
        )
    return dict(
        schema_version=1,
        asset_id=f"host:{item['kind']}:{item['name']}",
        kind=item["kind"],
        source_id="host",
        source_locator=f"host://{item['kind']}/{item['name']}",
        source_version=None,
        captured_at="1970-01-01T00:00:00Z",
        content_hash=hashlib.sha256(description.encode("utf-8")).hexdigest(),
        description=description,
        language_tags=[],
        dependencies=[],
        availability="unknown",
        stale=False,
        identity=identity,
    )


async def _third_party(run, request, settings, selector, text, recent, search) -> dict | None:
    """The selector's admitted result, or None after recording why not."""
    started = run.clock()
    offered: dict[str, Mapping] = {}
    # Word match answers in milliseconds, so a slow selector must leave it time to be the fallback.
    reserve = min(_FALLBACK_RESERVE_S, settings.total_ms / 1000 / 4) if settings.fallback else 0.0

    def check():
        if run.remaining() - reserve <= 0:
            raise TimeoutError

    async def call():
        assets = request["assets"]
        if len(assets) > MAX_OFFER_RECORDS:
            ranked = (await _rank(assets, tokenize(search))).items
            best = ranked[:MAX_OFFER_RECORDS]
            if len(best) < MAX_OFFER_RECORDS:  # Fill up with the rest only when matches run out.
                taken = {id(a) for a in best}
                best += [a for a in assets if id(a) not in taken][: MAX_OFFER_RECORDS - len(best)]
            assets = best

        def build():
            catalog = [_record(a, run.redact) for a in assets]
            offered.update((r["asset_id"], a) for r, a in zip(catalog, assets, strict=True))
            kinds = sorted({a["kind"] for a in assets}) or ["skill", "mcp_tool"]
            return parse_asset_request(
                dict(
                    schema_version=2,
                    recommendation_id=str(uuid.uuid4()),
                    task_id=f"guidance:{uuid.uuid4()}",
                    # Redaction can make a text a little longer than the request limit allows.
                    task_text=clip_bytes(text, MAX_TASK_BYTES),
                    # The latest words matter most, so a long earlier text keeps its end.
                    conversation=[dict(role="user", text=clip_end(recent, MAX_TASK_BYTES))]
                    if recent
                    else [],
                    scope={"user_id": "guidance", "workspace_id": "host"},
                    permissions=["assets.read"],
                    requested_kinds=kinds,
                    selection_scope="discovery",
                    catalog_revision=int(canonical_digest(catalog)[:12], 16),
                    host_snapshot_revision=None,
                    language_tags=["und"],
                    code_switched=False,
                    catalog=catalog,
                    deadline_ms=max(1, (run.remaining() - reserve) * 1000),
                    candidate_limit=10,
                )
            )

        # Building and checking up to 1,000 records takes a while; other requests keep the loop.
        v2 = await asyncio.to_thread(build) if len(assets) > _THREAD_ABOVE else build()
        raw = await selector.select(v2, SelectorContext(run.clock, check))
        parsed = parse_asset_result(raw)
        validate_against_offer(
            parsed, v2, SelectorIdentity(settings.name, settings.version), http=True
        )
        return parsed

    ok, parsed = await run.bounded("third_party", call, reserve=reserve)
    if not ok:
        return None
    run.record("third_party", "ok", started)
    data = parsed.data
    picks = [offered[c["asset_id"]] for c in data["candidates"]]
    return dict(picks=picks, mechanism=data["mechanism"])


async def run_guidance(
    request: Mapping,
    settings: GuidanceSettings,
    *,
    keywords: ChatFn | None = None,
    picker: ChatFn | None = None,
    selector: object | None = None,
    redact: Callable[[str], str] = lambda text: text,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """The stages of section 7.7, or the third-party selector with word match as its fallback."""
    run = _Run(settings, clock, redact)
    assets = request["assets"]
    limit = request.get("max_picks", settings.max_picks)
    # An undecided list is capped by max_choices, and never passes an explicit max_picks.
    choices = min(limit, settings.max_choices) if "max_picks" in request else settings.max_choices
    text = request["task_text"]
    clean = redact(text)
    recent = request.get("recent_text", "").strip()
    recent_clean = redact(recent)
    follow_up = bool(recent) and _word_count(text) <= _FOLLOW_UP_WORDS
    with_recent = f"{recent_clean}\n{clean}" if follow_up else clean

    if settings.primary == "http" and selector is not None:
        answer = await _third_party(
            run, request, settings, selector, clean, recent_clean, with_recent
        )
        if answer is not None:
            return _result(
                run,
                picks=answer["picks"][:limit],
                chosen_by="third_party",
                mechanism=answer["mechanism"],
                offered=len(assets),
                shortlist=len(answer["picks"]),
            )
        if not settings.fallback:
            return _result(
                run,
                picks=[],
                chosen_by="none",
                mechanism="third_party_failed",
                offered=len(assets),
                shortlist=0,
            )

    # Spec 7.7: after a selector failure only stage 1 runs, with no model call.
    fell_back = settings.primary == "http" and selector is not None
    started = clock()
    query = tokenize(text)
    search_text, search_clean = text, clean
    parts = _task_parts(text)
    ok, found = await run.bounded("word_match", lambda: _rank_tasks(assets, query, parts))
    # A short prompt whose own words find nothing strong may be a follow-up, such as "yes, do that".
    only_filler = _only_filler(text)
    again = True
    if ok and follow_up and (only_filler or not found.strong):
        wider = query | tokenize(recent)
        # One word_match stage in the result: a failure here is recorded for the whole stage.
        again, widened = await run.bounded(
            "word_match", lambda: _rank(assets, wider), started=started
        )
        # If the wider ranking fails, the first one stands, and the stage says why.
        if again:
            query, found = wider, widened
            search_text, search_clean = f"{recent}\n{text}", with_recent
    ranked = found if ok else _Ranked([], [])
    if ok and again:
        run.record("word_match", "ok", started)
    mechanism = "word_match"

    expanded = False

    async def expand() -> None:
        """Add a model's keywords to the words and rank again. A failure keeps what was found."""
        nonlocal expanded, mechanism, ranked
        expanded = True
        local = settings.keywords == "local"
        started = clock()
        messages = [
            dict(role="system", content=LOCAL_KEYWORD_PROMPT if local else KEYWORD_PROMPT),
            dict(role="user", content=search_clean),
        ]
        limit_tokens = _LOCAL_KEYWORD_TOKENS if local else _EXTERNAL_ANSWER_TOKENS
        ok, answer = await run.bounded("keywords", lambda: keywords(messages, limit_tokens))
        if not ok:
            return
        written, usage = answer
        if not written.strip():
            run.record("keywords", "failed", started, usage)
            return
        wider = query | tokenize(" ".join(written.split())[:2000])
        lines = [line for line in written[:2000].splitlines() if line.strip()]
        wider_parts = lines[:_MAX_TASK_PARTS] if len(lines) > 1 else parts
        ok, found = await run.bounded(
            "keywords",
            lambda: _rank_tasks(assets, wider, wider_parts),
            started=started,
            usage=usage,
        )
        if ok:
            ranked = found
            run.record("keywords", "ok", started, usage)
            mechanism = "word_match+keywords"

    can_expand = settings.keywords != "none" and keywords is not None and not fell_back
    if can_expand:
        if needs_keywords(search_text):
            await expand()
        else:
            run.record("keywords", "skipped")

    # A name the user wrote exactly is a clear request, wherever word match ranked it.
    named = _named_in_prompt(assets, text)
    named_ids = {id(item) for item in named}

    def named_first(items: Sequence[Mapping], size: int) -> list[Mapping]:
        rest = (item for item in items if id(item) not in named_ids)
        return [*named, *islice(rest, max(0, size - len(named)))][:size]

    def shortlist() -> list[Mapping]:
        return named_first(ranked.items, settings.shortlist)

    def unpicked_list() -> list[Mapping]:
        # A max_picks the request sets is a cap here too; the setting alone is not.
        size = min(settings.unpicked_limit, request.get("max_picks", settings.unpicked_limit))
        return named_first(ranked.strong, size)

    async def pick(short: Sequence[Mapping]) -> tuple[list[Mapping], bool] | None:
        started = clock()
        earlier = f"Earlier in this conversation:\n{recent_clean}\n\n" if recent_clean else ""
        messages = [
            dict(role="system", content=PICKER_PROMPT % choices),
            dict(
                role="user",
                content=f"List:\n{redact(_listing(short, redact))}\n\n{earlier}Request:\n{clean}",
            ),
        ]
        ok, answer = await run.bounded("picker", lambda: picker(messages, _EXTERNAL_ANSWER_TOKENS))
        if not ok:
            return None
        written, usage = answer
        try:
            chosen = _parse_picks(written, short)
        except (ValueError, RecursionError):  # An answer it cannot use, or nested too deep.
            run.record("picker", "failed", started, usage)
            return None
        run.record("picker", "ok", started, usage)
        return chosen

    use_picker = settings.picker != "none" and picker is not None and not fell_back
    short = shortlist() if use_picker else []
    # A prompt may share no word with the right asset. With nothing to show the picker yet, a
    # keyword stage that has not run runs first, as for an empty picker answer.
    if use_picker and not short and can_expand and not expanded:
        await expand()
        short = shortlist()
    if use_picker and short:
        answer = await pick(short)
        # The prompt may share no word with the right asset: try once more with synonyms.
        if answer is not None and not answer[0] and can_expand and not expanded:
            await expand()
            wider = shortlist()
            if wider != short:
                short = wider
                # The first answer stands if this call fails: it already said nothing fits.
                answer = await pick(short) or answer
        if answer is not None:
            chosen, unclear = answer
            unclear = unclear and len(chosen) > 1
            return _result(
                run,
                picks=chosen[: choices if unclear else limit],
                unclear=unclear,
                chosen_by="external_model",
                mechanism=f"{mechanism}+picker",
                offered=len(assets),
                shortlist=len(short),
            )
    if use_picker and not short:
        run.record("picker", "skipped")
    return _result(
        run,
        picks=unpicked_list(),
        chosen_by="word_match",
        mechanism=mechanism,
        offered=len(assets),
        shortlist=min(len(ranked.items), settings.shortlist),
    )
