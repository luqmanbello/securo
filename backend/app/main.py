import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.accounts import router as accounts_router
from app.api.budgets import router as budgets_router
from app.api.goals import router as goals_router
from app.api.groups import router as groups_router
from app.api.categories import router as categories_router
from app.api.category_groups import router as category_groups_router
from app.api.connections import router as connections_router
from app.api.custom_auth import router as custom_auth_router
from app.api.dashboard import router as dashboard_router
from app.api.import_logs import router as import_logs_router
from app.api.oidc_auth import router as oidc_auth_router
from app.api.passkeys import router as passkeys_router
from app.api.import_transactions import router as import_router
from app.api.info import router as info_router
from app.api.recurring_transactions import router as recurring_router
from app.api.rules import router as rules_router
from app.api.assets import router as assets_router
from app.api.asset_groups import router as asset_groups_router
from app.api.collections import router as collections_router
from app.api.reports import router as reports_router
from app.api.search import router as search_router
from app.api.setup import router as setup_router
from app.api.currencies import router as currencies_router
from app.api.export import router as export_router
from app.api.fx_rates import router as fx_rates_router
from app.api.attachments import router as attachments_router
from app.api.fiscal import router as fiscal_router
from app.api.invoice_attachments import router as invoice_attachments_router
from app.api.invoices import router as invoices_router
from app.api.public_invoices import router as public_invoices_router
from app.api.payees import router as payees_router
from app.api.settings import router as settings_router
from app.api.transactions import router as transactions_router
from app.api.two_factor import router as two_factor_router
from app.api.user_lookup import router as user_lookup_router
from app.api.workspaces import router as workspaces_router
from app.api.admin import router as admin_router, check_registration_enabled
from app.core.auth import fastapi_users
from app.core.auth_policy import require_local_auth_enabled
from app.core.config import get_settings
from app.core.rate_limit import login_rate_limit, register_rate_limit, password_reset_rate_limit
from app.core.redis import close_redis
from app.schemas.user import UserCreate, UserRead, UserUpdate

logger = logging.getLogger(__name__)
settings = get_settings()


async def _warm_tesouro_cache() -> None:
    """Pre-load the Tesouro Direto price cache so the first bond search is
    instant instead of waiting on the cold ~25s CSV download.

    Gated to instances that actually serve Brazilian users (a workspace with
    BRL as its default currency) so a non-Brazilian deployment never calls the
    Brazilian government endpoint just because the feature ships on by default.
    """
    try:
        if not get_settings().tesouro_direto_enabled:
            return
        from sqlalchemy import select

        from app.core.database import async_session_maker
        from app.models.workspace import Workspace

        async with async_session_maker() as session:
            has_brl = await session.scalar(
                select(Workspace.id).where(Workspace.default_currency == "BRL").limit(1)
            )
        if not has_brl:
            return

        from app.providers.tesouro_direto import get_tesouro_direto_provider

        await get_tesouro_direto_provider().get_available_bonds()
        logger.info("Startup: warmed Tesouro Direto price cache")
    except Exception:
        logger.exception("Startup: Tesouro Direto cache warm failed")


# Values that must never reach a real deployment. `config.py` ships the first
# as its default and `docker-compose.yml` supplies the second, so an unset or
# misnamed Secret does not fail — it silently falls back to a published string.
_INSECURE_SECRET_KEYS = {
    "",
    "change-me-in-production",
    "dev-secret-change-in-production",
}

# 256 bits of urlsafe base64 is ~43 chars. Anything materially shorter than
# that is either a placeholder or a hand-typed phrase.
_MIN_SECRET_KEY_LENGTH = 32


def _assert_secret_key_is_usable() -> None:
    """Refuse to serve on a guessable SECRET_KEY.

    This is a hard startup failure rather than a warning, because every way it
    goes wrong is invisible at runtime. SECRET_KEY signs the JWTs that
    authenticate every user (`app/core/auth.py`), signs password-reset and
    verification tokens, AND derives the Fernet key that encrypts stored bank
    credentials (`app/agents/services/crypto.py`) — using a salt that is a
    constant in this repository. So a default key means anyone who can read
    this source can mint a valid session for any user and decrypt any stored
    bank credential out of the database or a backup of it.

    None of that is visible from outside: the pods are Healthy, the UI works,
    and the only symptom is that the encryption was never real. A missing
    Secret has to be loud at boot or it is never noticed at all.

    In debug mode the placeholder is expected (that is what compose ships), so
    it warns instead — but it still warns, because a developer who connects a
    real bank locally is storing a real credential under a public key.
    """
    settings = get_settings()
    value = settings.secret_key.get_secret_value()
    insecure = value in _INSECURE_SECRET_KEYS
    too_short = len(value) < _MIN_SECRET_KEY_LENGTH

    if not (insecure or too_short):
        return

    reason = (
        "SECRET_KEY is a known placeholder"
        if insecure
        else f"SECRET_KEY is shorter than {_MIN_SECRET_KEY_LENGTH} characters"
    )

    if settings.debug:
        logger.warning(
            "%s. This is tolerated because DEBUG is on, but sessions are "
            "forgeable and any stored bank credential is decryptable by "
            "anyone with this source. Do not connect a real bank.",
            reason,
        )
        return

    raise RuntimeError(
        f"{reason}. Refusing to start: it signs every session token and "
        "derives the key that encrypts stored bank credentials, so a "
        "guessable value means forgeable logins and recoverable credentials. "
        "Set SECRET_KEY to at least "
        f"{_MIN_SECRET_KEY_LENGTH} characters from a CSPRNG, e.g. "
        "python3 -c 'import secrets; print(secrets.token_urlsafe(64))'."
    )


# The agents feature ships its own shared secret with its own placeholder,
# declared in `app/agents/config.py` rather than here.
_INSECURE_MCP_JWT_SECRETS = frozenset(
    {"", "change-me-in-production", "dev-mcp-secret-change-in-production"}
)
_MIN_MCP_JWT_SECRET_LENGTH = 32


def _assert_mcp_jwt_secret_is_usable() -> None:
    """Refuse to serve with agents on and a guessable AGENTS_MCP_JWT_SECRET.

    Only checked when `AGENTS_ENABLED` is true: with agents off the router is
    not mounted, the mcp-server container is not deployed, and the value
    signs nothing.

    With agents on it is the *only* thing standing in front of the MCP
    server. `mcp_server/auth.py` authenticates every tool call by verifying
    an HS256 JWT against this secret and nothing else — there is no second
    factor, no allowlist, and no network assumption baked into the check.
    The tools it guards read and write real financial data (transactions,
    accounts, budgets, payees, proposals) directly against the database, and
    a chart that publishes the server behind an ingress makes that endpoint
    reachable by anything that can resolve the host.

    So a placeholder here is worse than a weak password: the value is
    printed in this repository, which means anyone who can read the source
    can mint a token that the server accepts. Like SECRET_KEY, every way it
    goes wrong is invisible — the pods are Healthy, the agent works, and the
    only symptom is that the authentication was never real.

    Deliberately mirrors `_assert_secret_key_is_usable` rather than sharing
    with it: the two secrets have different names, different defaults and
    independent revocation, and collapsing them would make one failure
    message describe the wrong key.
    """
    from app.agents.config import get_agent_settings

    agent_settings = get_agent_settings()
    if not agent_settings.enabled:
        return

    value = agent_settings.mcp_jwt_secret or ""
    insecure = value in _INSECURE_MCP_JWT_SECRETS
    too_short = len(value) < _MIN_MCP_JWT_SECRET_LENGTH

    if not (insecure or too_short):
        return

    reason = (
        "AGENTS_MCP_JWT_SECRET is a known placeholder"
        if insecure
        else (
            "AGENTS_MCP_JWT_SECRET is shorter than "
            f"{_MIN_MCP_JWT_SECRET_LENGTH} characters"
        )
    )

    if get_settings().debug:
        logger.warning(
            "%s, while AGENTS_ENABLED is true. This is tolerated because "
            "DEBUG is on, but anyone who can reach the MCP server can forge "
            "a token and read or modify financial data. Do not expose it.",
            reason,
        )
        return

    raise RuntimeError(
        f"{reason}, while AGENTS_ENABLED is true. Refusing to start: this "
        "secret is the only thing authenticating MCP tool calls, and those "
        "tools read and write transactions, accounts and budgets directly. "
        "A placeholder is published in this repository, so anyone who can "
        "read the source can mint a token the server accepts. Set "
        "AGENTS_MCP_JWT_SECRET to at least "
        f"{_MIN_MCP_JWT_SECRET_LENGTH} characters from a CSPRNG, e.g. "
        "python3 -c 'import secrets; print(secrets.token_urlsafe(64))' — or "
        "set AGENTS_ENABLED=false if you are not using the agents feature."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else, and before any request can be served.
    _assert_secret_key_is_usable()
    _assert_mcp_jwt_secret_is_usable()
    # Startup: dispatch sync for all stale bank connections
    try:
        from app.worker import celery_app  # noqa: F811

        celery_app.send_task("app.tasks.sync_tasks.sync_all_connections")
        logger.info("Startup: dispatched sync_all_connections task to Celery")
    except Exception:
        logger.exception("Startup: failed to dispatch sync task")
    # Background pre-warm of the Tesouro cache (non-blocking; gated to BRL
    # instances inside the helper). Kept on app.state so it isn't GC'd.
    app.state.tesouro_warm_task = asyncio.create_task(_warm_tesouro_cache())
    yield
    # Shutdown
    await close_redis()


app = FastAPI(
    title=settings.app_name,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth routes — custom login/logout with 2FA support (mounted first to take precedence)
app.include_router(
    custom_auth_router,
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(login_rate_limit)],
)
app.include_router(
    two_factor_router,
    prefix="/api/auth",
    tags=["auth"],
)
app.include_router(
    passkeys_router,
    prefix="/api/auth",
    tags=["auth"],
)
app.include_router(oidc_auth_router)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[
        Depends(require_local_auth_enabled),
        Depends(check_registration_enabled),
        Depends(register_rate_limit),
    ],
)
app.include_router(
    fastapi_users.get_reset_password_router(),
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(require_local_auth_enabled), Depends(password_reset_rate_limit)],
)
# user_lookup must precede the fastapi-users router below so the
# `/api/users/lookup` path isn't captured by the catch-all `/{id}`
# route fastapi-users mounts.
app.include_router(user_lookup_router)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate),
    prefix="/api/users",
    tags=["users"],
)

# Domain routes
app.include_router(categories_router)
app.include_router(category_groups_router)
app.include_router(rules_router)
app.include_router(transactions_router)
app.include_router(import_router)
app.include_router(import_logs_router)
app.include_router(accounts_router)
app.include_router(connections_router)
app.include_router(recurring_router)
app.include_router(budgets_router)
app.include_router(goals_router)
app.include_router(groups_router)
app.include_router(assets_router)
app.include_router(asset_groups_router)
app.include_router(collections_router)
app.include_router(dashboard_router)
app.include_router(reports_router)
app.include_router(search_router)
app.include_router(setup_router)
app.include_router(currencies_router)
app.include_router(fx_rates_router)
app.include_router(export_router)
app.include_router(attachments_router)
app.include_router(fiscal_router)
app.include_router(payees_router)
app.include_router(invoices_router)
app.include_router(invoice_attachments_router)
app.include_router(public_invoices_router)
app.include_router(settings_router)
app.include_router(workspaces_router)
app.include_router(admin_router)
app.include_router(info_router)


# Optional agents/MCP/LLM module — fully gated by AGENTS_ENABLED so users
# who don't want this feature pay zero cost (no imports, no routes, no
# background tasks). The module itself is self-contained in app/agents/.
if os.getenv("AGENTS_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on"):
    try:
        from app.agents.api.info import router as agents_info_router
        from app.agents.api.agents import router as agents_router
        from app.agents.api.connections import router as agents_connections_router
        from app.agents.api.conversations import router as agents_conversations_router
        from app.agents.api.chat import router as agents_chat_router
        from app.agents.api.knowledge import router as agents_knowledge_router
        from app.agents.api.mcp_tokens import router as agents_mcp_tokens_router

        # Mount literal-prefix routers (conversations, connections,
        # mcp-tokens) BEFORE the generic agents router so paths like
        # /api/agents/connections don't get captured by /api/agents/{agent_id}.
        app.include_router(agents_info_router)
        app.include_router(agents_connections_router)
        app.include_router(agents_conversations_router)
        app.include_router(agents_mcp_tokens_router)
        app.include_router(agents_router)
        app.include_router(agents_chat_router)
        app.include_router(agents_knowledge_router)
        logger.info("Agents feature enabled — mounted /api/agents routes")
    except Exception:
        logger.exception("Agents feature flag is on but import failed; routes not mounted")


@app.get("/api/health")
async def health_check():
    return {"status": "healthy"}
