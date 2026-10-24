"""Grafana's template interpolation for a SQL panel, as the dashboard tests need it.

Grafana interpolates template variables in the browser, so a test cannot ask the server how a
panel query renders. This models grafana-oss 10.4.0's frontend rules (template_srv.ts, the
variable format registry and the SQL datasource's ``interpolateVariable``), checked against the
shipped 10.4.0 bundle and against the SQL a real browser sent to /api/ds/query:

* ``${var:sqlstring}`` -> ``'`` + the value with ``'`` doubled and ``"`` as ``\\"`` + ``'``;
* ``${var:percentencode}`` -> ``encodeURIComponent``, then ``!'()*`` percent-encoded too, so
  only ``A-Z a-z 0-9 - _ . ~ %`` remain;
* ``${var}`` / ``$var`` with no format -> the SQL datasource's own rule: the value with ``'``
  doubled, wrapped in quotes when the variable is ``multi`` or ``includeAll``. So
  ``'${var}'`` on an includeAll variable renders ``''value''``: a syntax error for a plain
  value, and the value's quote-free text lands outside any string literal;
* a variable at All with a custom ``allValue`` -> the allValue goes in AS IS, whatever the
  format, except ``percentencode`` (which encodes it) and ``text`` (which gives ``All``).
"""
import re
from typing import Dict, Iterable, Optional
from urllib.parse import quote

ALL = "$__all"

_REF = re.compile(r"\$\{(\w+)(?::(\w+))?\}|\$(\w+)\b")
# A Postgres string literal (standard_conforming_strings on: '' is the only escape).
_LITERAL = re.compile(r"'(?:[^']|'')*'")


def percentencode(value: str) -> str:
    """Grafana's ``percentencode``: encodeURIComponent plus ``!'()*``."""
    return quote(value, safe="-_.~")


def sqlstring(value: str) -> str:
    """Grafana's ``sqlstring`` for one value."""
    return "'" + value.replace("'", "''").replace('"', '\\"') + "'"


def _format(var: dict, value: str, fmt: Optional[str]) -> str:
    if value == ALL:
        all_value = var.get("allValue")
        assert var.get("includeAll") and all_value is not None, (
            f"{var.get('name')}: All without a custom allValue is not modelled")
        if fmt == "text":
            return "All"
        if fmt != "percentencode":
            return all_value                           # the custom allValue, unformatted
        value = all_value
    if fmt == "sqlstring":
        return sqlstring(value)
    if fmt == "percentencode":
        return percentencode(value)
    if fmt is None:
        doubled = value.replace("'", "''")
        return f"'{doubled}'" if (var.get("multi") or var.get("includeAll")) else doubled
    raise AssertionError(f"format {fmt!r} is not modelled")


def render(sql: str, variables: Iterable[dict], values: Dict[str, str],
           time_from: str = "", time_to: str = "") -> str:
    """``sql`` as Grafana 10.4 sends it, with ``values`` (``ALL`` for All) for the dashboard's
    ``variables``; ``$__timeFilter(col)`` becomes ``col BETWEEN time_from AND time_to`` and
    ``$__timeFrom()`` / ``$__timeTo()`` the quoted bounds (the server-side macros)."""
    defs = {v["name"]: v for v in variables}
    sql = re.sub(r"\$__timeFilter\((\w+)\)",
                 lambda m: f"{m.group(1)} BETWEEN '{time_from}' AND '{time_to}'", sql)
    sql = sql.replace("$__timeFrom()", f"'{time_from}'").replace("$__timeTo()", f"'{time_to}'")

    def one(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        if name.startswith("__"):
            return m.group(0)
        assert name in values, f"no value for ${name}"
        return _format(defs.get(name, {"name": name}), values[name], m.group(2))

    sql = _REF.sub(one, sql)
    leftover = re.findall(r"\$\{?\w+", sql)
    assert not leftover, f"unrendered {leftover} in: {sql}"
    return sql


def skeleton(sql: str) -> str:
    """``sql`` with every string literal replaced by ``?``: what is left is the SQL itself."""
    return _LITERAL.sub("?", sql)
