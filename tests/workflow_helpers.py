"""Shared checks for the GitHub Actions expression subset used by our workflows."""

import json
import re


def github_expression(text: str, context: dict):
    """Evaluate the small GitHub Actions expression subset the CI workflow uses.

    Supports property paths, single-quoted strings, ``!``, ``==``, ``!=``,
    ``&&`` and ``||`` with the documented value-returning semantics; a missing
    property is null and strings compare ignoring case, as on GitHub.
    """
    wrapped = re.fullmatch(r"\s*\$\{\{(.*?)\}\}\s*", text, re.DOTALL)
    if wrapped:
        text = wrapped.group(1).strip()

    pattern = r"\|\||&&|==|!=|!|\(|\)|'[^']*'|[A-Za-z_][\w.-]*"
    tokens = re.findall(pattern, text)
    assert re.sub(pattern, "", text).strip() == "", text
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def primary():
        token = take()
        if token == "fromJSON":
            assert take() == "("
            value = either()
            assert take() == ")"
            return json.loads(value)
        if token == "(":
            value = either()
            assert take() == ")"
            return value
        if token == "!":
            return not primary()
        if token.startswith("'"):
            return token[1:-1]
        value = context
        for part in token.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def fold(value):
        return value.casefold() if isinstance(value, str) else value

    def compare():
        left = primary()
        while peek() in ("==", "!="):
            op, right = take(), primary()
            equal = fold(left) == fold(right)
            left = equal if op == "==" else not equal
        return left

    def both():
        left = compare()
        while peek() == "&&":
            take()
            right = compare()
            left = right if left else left
        return left

    def either():
        left = both()
        while peek() == "||":
            take()
            right = both()
            left = left if left else right
        return left

    value = either()
    assert pos == len(tokens), text
    return value
