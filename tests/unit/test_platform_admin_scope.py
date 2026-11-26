"""A key bound to a team is never a platform admin (owner decision 82: own team, own tenant)."""
import pytest

from auth.api_key_manager import is_admin_key, is_platform_admin


@pytest.mark.parametrize("meta, expected", [
    ({"tenant_id": "ops", "admin": True}, True),
    ({"tenant_id": "ops", "admin": True, "team": "  "}, True),
    ({"tenant_id": "acme", "admin": True, "team": "payments"}, False),
    ({"tenant_id": "acme", "team": "payments"}, False),
    ({"tenant_id": "acme"}, False),
    (None, False),
])
def test_is_platform_admin(meta, expected):
    assert is_platform_admin(meta) is expected


def test_team_admin_keeps_the_key_level_admin_flag():
    # /admin/keys and /admin/routing still admit it, scoped to its team (admin_scope).
    assert is_admin_key({"tenant_id": "acme", "admin": True, "team": "payments"})


def test_cross_tenant_call_sites_use_the_platform_check():
    """Source guard: only the team-scoped admin APIs may use the plain admin flag."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "src" / "proxy"
    main = (root / "main.py").read_text(encoding="utf-8")
    assert 'params["_auth_admin"] = is_platform_admin(tenant_metadata)' in main
    assert "key_is_admin=is_platform_admin(tenant_metadata)" in main
    assert "if not is_platform_admin(tenant_metadata) and owner !=" in main
    for action in ('"cross-tenant tool governance"', '"alert webhook ingestion"'):
        assert f"_require_platform_admin(tenant_metadata, {action})" in main
