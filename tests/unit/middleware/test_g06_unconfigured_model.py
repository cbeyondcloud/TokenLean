"""G6_routing.on_unconfigured_model: substitute (default, unchanged) or reject.

With routing off (or no tiers), a model the config does not know is served by
proxy.default_model, disclosed in x-tokenlean-model-substituted. A caller whose answer quality
is measured per model (an agent pipeline) needs to hear "no" instead: with `reject`, G06 raises
UnconfiguredModelError and the route answers 400 without calling any provider.
"""
import pytest

import middleware.g06_routing as g06
from middleware.g06_routing import G06Routing, UnconfiguredModelError


def _prep(monkeypatch, ctx, configured, **g6):
    monkeypatch.setattr(g06, "_is_configured_model", lambda m: configured)
    monkeypatch.setattr(g06, "get_default_model", lambda: "gpt-4o-mini")
    ctx.config["groups"]["G6_routing"] = dict(g6)
    return ctx


async def test_default_still_substitutes(make_ctx, monkeypatch):
    ctx = _prep(monkeypatch, make_ctx(model="my-model-x"), False, enabled=False)
    await G06Routing().process_request(ctx)
    assert ctx.model_substituted == "gpt-4o-mini"


async def test_reject_raises_for_an_unconfigured_model_when_routing_is_off(make_ctx, monkeypatch):
    ctx = _prep(monkeypatch, make_ctx(model="my-model-x"), False,
                enabled=False, on_unconfigured_model="reject")
    with pytest.raises(UnconfiguredModelError) as err:
        await G06Routing().process_request(ctx)
    assert err.value.model == "my-model-x"
    assert ctx.model == "my-model-x" and ctx.model_substituted == ""


async def test_reject_raises_on_the_no_tiers_path_too(make_ctx, monkeypatch):
    ctx = _prep(monkeypatch, make_ctx(model="my-model-x"), False,
                enabled=True, tiers={}, on_unconfigured_model="reject")
    monkeypatch.setattr(g06, "_resolve_tiers", lambda *a, **k: {})
    with pytest.raises(UnconfiguredModelError):
        await G06Routing().process_request(ctx)


async def test_reject_leaves_a_configured_model_alone(make_ctx, monkeypatch):
    ctx = _prep(monkeypatch, make_ctx(model="o4-mini"), True,
                enabled=False, on_unconfigured_model="reject")
    await G06Routing().process_request(ctx)
    assert ctx.model == "o4-mini" and ctx.model_substituted == ""


async def test_unrecognised_value_keeps_todays_behaviour(make_ctx, monkeypatch):
    ctx = _prep(monkeypatch, make_ctx(model="my-model-x"), False,
                enabled=False, on_unconfigured_model="refuse-please")
    await G06Routing().process_request(ctx)
    assert ctx.model_substituted == "gpt-4o-mini"
