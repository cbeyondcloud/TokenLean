"""Patterns that run on client text inside the event loop must take linear time.

A pattern where two quantifiers can split the same run of characters backtracks over every
split once the rest fails to match: `^\\s+` under MULTILINE spans the newlines that `^`
starts on, and `\\s*\\{?\\s*` splits one run of spaces two ways. Both took seconds on the
crafted inputs below and grew with the square of the length, stalling every request on the
worker. The one-second bound is generous so the test measures the shape, not the machine.
"""
import pathlib
import sys
import time

import pytest

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

from middleware.g19_headroom import _fence_language  # noqa: E402


def _quick(fn, *args) -> bool:
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start < 1.0


class TestG19FenceLanguage:
    """G19 reads the language of every fence it compresses, in any role's content."""

    @pytest.mark.parametrize("line, language", [
        ("```python", "python"), ("``` python", "python"), ("```{python}", "python"),
        ("``` { python", "python"), ("  ```js", "js"), ("````TS", "ts"), ("```c++", "c++"),
        ("```", ""), ("```   ", ""), ("text", ""),
    ])
    def test_the_language_is_read(self, line, language):
        assert _fence_language(line) == language

    @pytest.mark.parametrize("line", [
        "```" + " " * 60_000 + "!", "```" + "\t" * 60_000 + "!", "```{" + " " * 60_000 + "!",
        "```" + " " * 30_000 + "{" + " " * 30_000 + "!", " " * 60_000 + "!",
        "`" * 60_000 + "!",
    ], ids=lambda t: repr(t[:6]))
    def test_it_is_quick_on_crafted_input(self, line):
        assert _quick(_fence_language, line)
