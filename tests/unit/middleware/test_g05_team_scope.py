"""G05 cache keys and team overlays (ST-1 review).

A team's config overlay can change what a request is answered with (its default model, a
group's knobs), and the default cache scope is the whole tenant. So an answer stored under
one team's overlay must not be served to a request that ran under another overlay, or
under none: the overlay's fingerprint (``ctx.team_overlay_tag``) is folded into the L1 key
source and the L2 scope. Without an overlay, keys are byte-identical to before, so teams
without an overlay share the tenant's cache as every request did.
"""
from middleware.g05_cache import _apply_model_scope, _scope_value


def _pair(make_ctx, tag):
    ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
    ctx.team_overlay_tag = tag
    return _apply_model_scope("q", ctx), _scope_value(ctx)


def test_no_overlay_keeps_the_keys_byte_identical(make_ctx):
    l1, l2 = _pair(make_ctx, "")
    ctx = make_ctx([{"role": "user", "content": "What is the capital of France?"}])
    assert (l1, l2) == (_apply_model_scope("q", ctx), _scope_value(ctx))
    assert "team=" not in l1 and "team=" not in l2


def test_an_overlay_scopes_both_levels(make_ctx):
    none = _pair(make_ctx, "")
    a = _pair(make_ctx, "a" * 16)
    b = _pair(make_ctx, "b" * 16)
    assert len({none[0], a[0], b[0]}) == 3            # L1: three different keys
    assert len({none[1], a[1], b[1]}) == 3            # L2: three different scopes
    assert "team=" + "a" * 16 in a[1]


def test_two_teams_with_the_same_overlay_share(make_ctx):
    assert _pair(make_ctx, "c" * 16) == _pair(make_ctx, "c" * 16)
