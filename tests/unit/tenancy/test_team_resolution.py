"""Which team a request belongs to (ST-1).

The key's team binding is authoritative: it wins over a gateway key's X-Team, and an
ordinary key's X-Team is still ignored. A key with no binding behaves exactly as before
(gateway → X-Team, everyone else → "default").
"""
from tenancy.resolver import DEFAULT_TEAM, apply_caller_identity, resolve_team


def test_a_bound_key_s_team_wins_over_any_header():
    assert resolve_team("finance", key_is_gateway=False, key_team="payments") == "payments"
    assert resolve_team("finance", key_is_gateway=True, key_team="payments") == "payments"
    assert resolve_team(None, key_is_gateway=False, key_team="payments") == "payments"


def test_without_a_binding_the_old_rule_holds():
    assert resolve_team("finance", key_is_gateway=False) == DEFAULT_TEAM
    assert resolve_team("finance", key_is_gateway=True) == "finance"
    assert resolve_team("finance", key_is_gateway=True, key_team=None) == "finance"
    assert resolve_team("finance", key_is_gateway=False, key_team="") == DEFAULT_TEAM
    assert resolve_team("", key_is_gateway=True, key_team="  ") == DEFAULT_TEAM


class _Ctx:
    def __init__(self, **params):
        self.params = params
        self.user_id = "dev@acme.test"


def test_apply_caller_identity_reads_the_key_binding():
    c = _Ctx(_auth_principal="acme", _auth_tenant_id="acme", _auth_team="payments")
    apply_caller_identity(c, {"x-team": "finance"})
    assert c.team == "payments"


def test_apply_caller_identity_without_binding_is_unchanged():
    c = _Ctx(_auth_principal="acme", _auth_tenant_id="acme")
    apply_caller_identity(c, {"x-team": "finance"})
    assert c.team == DEFAULT_TEAM
    g = _Ctx(_auth_principal="edge", _auth_tenant_id="acme", _auth_gateway=True)
    apply_caller_identity(g, {"x-team": "finance"})
    assert g.team == "finance"


def test_a_body_field_cannot_bind_a_team():
    # main._serve_core overwrites _auth_* for every key; this guards the rule's input: a
    # non-string binding (e.g. smuggled JSON) is no binding.
    c = _Ctx(_auth_principal="acme", _auth_team=["payments"])
    apply_caller_identity(c, {})
    assert c.team == DEFAULT_TEAM
