"""G5_cache.l2_enabled: switch the semantic (L2) tier off and keep L1 exact-match caching.

An embedding match can serve one question's answer for a different question (the module's
own notes: "available"/"not available" score 0.97), so a deployment that wants caching only
for byte-identical requests needs L2 off on its own, without disabling G05. Default true:
nothing changes unless an operator sets it.
"""
import pytest

from middleware import g05_cache
from middleware.g05_cache import _l2_lookup, _l2_store, _semantic_cache_disabled, l2_enabled

_SINGLE_TURN = [{"role": "system", "content": "s"}, {"role": "user", "content": "capital of France?"}]


class _Reached(Exception):
    pass


async def _embed_reached(*a, **k):
    raise _Reached()


def _cfg(**g5):
    return {"groups": {"G5_cache": dict(g5)}}


@pytest.mark.parametrize("value,expected", [
    (None, True), (True, True), (False, False), ("false", False), ("off", False), ("0", False),
])
def test_l2_enabled_parsing(value, expected):
    g5 = {} if value is None else {"l2_enabled": value}
    assert l2_enabled(g5) is expected


def test_l2_off_disables_semantic_for_a_single_turn_request(make_ctx):
    ctx = make_ctx(messages=_SINGLE_TURN, config=_cfg(l2_enabled=False))
    assert _semantic_cache_disabled(ctx) is True


def test_l2_default_keeps_semantic_for_a_single_turn_request(make_ctx):
    ctx = make_ctx(messages=_SINGLE_TURN, config=_cfg())
    assert _semantic_cache_disabled(ctx) is False


@pytest.mark.asyncio
async def test_l2_off_never_embeds_on_lookup(make_ctx, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/none")
    monkeypatch.setattr(g05_cache, "_embed", _embed_reached)
    ctx = make_ctx(messages=_SINGLE_TURN, config=_cfg(l2_enabled=False))
    assert await _l2_lookup(ctx, 0.95) == (None, 0.0)


@pytest.mark.asyncio
async def test_l2_default_does_embed_on_lookup(make_ctx, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/none")
    monkeypatch.setattr(g05_cache, "_embed", _embed_reached)
    ctx = make_ctx(messages=_SINGLE_TURN, config=_cfg())
    with pytest.raises(_Reached):
        await _l2_lookup(ctx, 0.95)


@pytest.mark.asyncio
async def test_l2_off_never_embeds_on_store(make_ctx, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/none")
    monkeypatch.setattr(g05_cache, "_embed", _embed_reached)
    ctx = make_ctx(messages=_SINGLE_TURN, config=_cfg(l2_enabled=False))
    response = {"choices": [{"message": {"role": "assistant", "content": "Paris"}}]}
    assert await _l2_store(ctx, response, 60) is None
