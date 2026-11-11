"""Small rewrites on assume text (the `assumes:` expressions of the config), shared by the formal
engine (N2b/N3) and the generated testbench (N3).

  desugar      $stable(e) -> ((e) == $past(e)), $changed(e) -> ((e) != $past(e)),
               $rose(e) -> ((e) && !$past(e)), $fell(e) -> (!(e) && $past(e))
  past_calls   every `$past(e)` / `$past(e, k)` with its span, argument and depth
"""
from __future__ import annotations

import re

_CALL = re.compile(r"\$(past|stable|changed|rose|fell)\s*\(")


def _close(text: str, i: int) -> int:
    """Index of the parenthesis closing the one at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError(f"括号不匹配：{text}")


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def desugar(text: str) -> str:
    while True:
        m = next((m for m in _CALL.finditer(text) if m.group(1) != "past"), None)
        if m is None:
            return text
        end = _close(text, m.end() - 1)
        e = desugar(text[m.end():end])
        rep = {"stable": f"(({e}) == $past({e}))", "changed": f"(({e}) != $past({e}))",
               "rose": f"(({e}) && !$past({e}))", "fell": f"(!({e}) && $past({e}))"}[m.group(1)]
        text = text[:m.start()] + rep + text[end + 1:]


def past_calls(text: str) -> list[tuple[int, int, str, int]]:
    """[(start, end_exclusive, argument, depth)] of the outermost $past calls."""
    out, i = [], 0
    while True:
        m = _CALL.search(text, i)
        if m is None:
            return out
        end = _close(text, m.end() - 1)
        if m.group(1) == "past":
            args = _split_args(text[m.end():end])
            k = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
            out.append((m.start(), end + 1, args[0], k))
        i = end + 1
