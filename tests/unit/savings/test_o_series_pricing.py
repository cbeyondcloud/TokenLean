"""The shipped pricing table prices the o-series at OpenAI's list (backlog #146, #149).

`o3-mini` and `o4-mini` had no row, so `o4-mini` priced at `default` ($0.005 / $0.015 per 1K, equal to
gpt-4o) and `o3-mini` matched the `o3` row by substring - which was $0.01 / $0.04, five times OpenAI's
list of $2 / $8 per million. G06's cost guards read these rows. Prices below are OpenAI's pricing
page as read on 2026-10-16 ($ per 1M: o3-mini 1.10 / 4.40, o4-mini 1.10 / 4.40, o3 2.00 / 8.00).
"""
import pathlib
import sys

import pytest
import yaml
from unittest.mock import patch

_PROXY = pathlib.Path(__file__).resolve().parents[3] / "src" / "proxy"
if str(_PROXY) not in sys.path:
    sys.path.insert(0, str(_PROXY))

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _template_pricing():
    return yaml.safe_load((ROOT / "config" / "config.yaml.template").read_text(encoding="utf-8"))["pricing"]


def _price(model):
    from savings.calculator import get_cost_per_1k
    with patch("config_loader.get_pricing_table", return_value=_template_pricing()):
        return get_cost_per_1k(model)


@pytest.mark.parametrize("model,expected", [
    ("o4-mini", (0.0011, 0.0044)),
    ("o4-mini-2025-04-16", (0.0011, 0.0044)),
    ("o3-mini", (0.0011, 0.0044)),
    ("o3-mini-high", (0.0011, 0.0044)),
    ("o3", (0.002, 0.008)),
])
def test_the_o_series_is_priced_at_openai_list(model, expected):
    assert _price(model) == pytest.approx(expected)


def test_o3_mini_no_longer_resolves_to_the_o3_row():
    """Longest matching key wins, so the dedicated row must beat the `o3` substring."""
    assert _price("o3-mini") != _price("o3")


def test_the_o_minis_are_cheaper_than_gpt_4o_which_is_what_the_cost_guards_assume():
    gpt4o = _price("gpt-4o")
    for model in ("o3-mini", "o4-mini"):
        in_cost, out_cost = _price(model)
        assert in_cost < gpt4o[0] and out_cost < gpt4o[1], model


def test_gpt_4o_mini_is_still_priced_as_itself_not_as_gpt_4o():
    assert _price("gpt-4o-mini") == pytest.approx((0.00015, 0.0006))


def test_no_o_series_model_falls_to_default_any_more():
    default = _template_pricing()["default"]
    for model in ("o4-mini", "o3-mini", "o3", "o1"):
        assert _price(model) != (default["input"], default["output"]), model


def test_the_harness_base_config_prices_them_the_same():
    """Every ablation arm is built from this file; if it priced the o-series differently the
    published cost figures would describe a table that does not ship."""
    base = ROOT / "config" / "config.roi-openai.yaml"
    if not base.exists():
        pytest.skip("the harness base config is not part of this checkout")
    harness = yaml.safe_load(base.read_text(encoding="utf-8"))["pricing"]
    shipped = _template_pricing()
    for key in ("o3", "o3-mini", "o4-mini", "o1"):
        assert harness[key] == shipped[key], key
