import pytest

from app.core.errors import AuthError, ForbiddenError
from app.core.security import Principal, get_principal


def test_admin_bypasses_role_check():
    Principal(subject="x", tenant_id="t", roles=["admin"]).require_role("kb.author")


def test_missing_role_raises():
    with pytest.raises(ForbiddenError):
        Principal(subject="x", tenant_id="t", roles=["user"]).require_role("admin")


@pytest.mark.asyncio
async def test_unknown_api_key_rejected():
    with pytest.raises(AuthError):
        await get_principal(authorization=None, x_api_key="not-a-real-key", x_tenant_id="t")


@pytest.mark.asyncio
async def test_valid_api_key_gets_service_principal():
    principal = await get_principal(authorization=None, x_api_key="local-dev-key", x_tenant_id="acme")
    assert principal.is_service and principal.tenant_id == "acme"
