from importlib import import_module

from app.core.config import Settings
from app.providers.logos import ACCESSBANK_LOGO_URL, BYBIT_LOGO_URL
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectTokenData,
    ConnectionData,
    HoldingData,
    InstitutionData,
    InstitutionListData,
    ProviderUserActionRequired,
    RefreshOutcome,
    SessionExpiredError,
    TransactionData,
)
from app.providers.credentials import is_configured

# Registry of available providers.
_PROVIDERS: dict[str, type[BankProvider]] = {}

# All known providers the system supports (extensible for future connectors).
KNOWN_PROVIDERS = [
    {
        "name": "pluggy",
        "display_name": "Pluggy",
        "description": "Open finance provider for Brazilian banks",
        "flow_type": "widget",
        "requires_institution_select": False,
        "supports_asset_sync": True,
    },
    {
        "name": "enable_banking",
        "display_name": "Enable Banking",
        "description": "European banks via PSD2 open banking",
        "flow_type": "oauth",
        "requires_institution_select": True,
        "supports_asset_sync": False,
    },
    {
        "name": "simplefin",
        "display_name": "SimpleFIN",
        "description": "US and international banks via SimpleFIN Bridge",
        "flow_type": "token",
        "requires_institution_select": False,
        "supports_asset_sync": True,
    },
    {
        "name": "accessbank",
        "display_name": "Access Bank",
        "description": "Nigerian bank via internet-banking, balances and transactions",
        "flow_type": "credentials",
        "requires_institution_select": False,
        "supports_asset_sync": False,
        "logo_url": ACCESSBANK_LOGO_URL,
    },
    {
        "name": "bybit",
        "display_name": "Bybit",
        "description": "Bybit stablecoin balance and Bybit Card payments via a read-only API key",
        "flow_type": "credentials",
        "requires_institution_select": False,
        "supports_asset_sync": False,
        "logo_url": BYBIT_LOGO_URL,
        # The credentials dialog renders these instead of its user ID +
        # password default. `secret` fields are masked and never autofilled.
        "credential_fields": [
            {
                "name": "api_key",
                "label_key": "accounts.credentialsConnect.bybit.apiKeyLabel",
                "placeholder_key": "accounts.credentialsConnect.bybit.apiKeyPlaceholder",
                "secret": False,
            },
            {
                "name": "api_secret",
                "label_key": "accounts.credentialsConnect.bybit.apiSecretLabel",
                "placeholder_key": "accounts.credentialsConnect.bybit.apiSecretPlaceholder",
                "secret": True,
            },
        ],
    },
]


def register_provider(name: str, cls: type[BankProvider]) -> None:
    """Register a bank provider implementation."""
    _PROVIDERS[name] = cls


def get_provider(name: str, settings: Settings | None = None) -> BankProvider:
    """Get an instance of a registered bank provider by name."""
    available_providers = _available_providers(settings)
    provider_class = available_providers.get(name)
    if not provider_class:
        available = ", ".join(available_providers) or "(none)"
        raise ValueError(f"Unknown provider: {name}. Available: {available}")
    return provider_class(settings=settings)


def all_known_providers(settings: Settings | None = None) -> list[dict]:
    """Return all known providers with a configured flag."""
    available = _available_providers(settings)
    return [{**p, "configured": p["name"] in available} for p in KNOWN_PROVIDERS]


# Providers enabled by instance settings, imported only once configured.
_CREDENTIALED_PROVIDERS = {
    "pluggy": ("app.providers.pluggy", "PluggyProvider"),
    "enable_banking": ("app.providers.enable_banking", "EnableBankingProvider"),
    "simplefin": ("app.providers.simplefin", "SimpleFinProvider"),
}

# Fork-only providers, switched on by an env flag rather than Admin settings.
_FLAGGED_PROVIDERS = {
    "accessbank": ("accessbank_enabled", "app.providers.accessbank", "AccessBankProvider"),
    "bybit": ("bybit_enabled", "app.providers.bybit", "BybitProvider"),
}


def _available_providers(settings: Settings | None = None) -> dict[str, type[BankProvider]]:
    """Derive availability without mutating process-wide registration."""
    from app.core.config import get_settings

    settings = settings or get_settings()
    providers = dict(_PROVIDERS)
    for name, (module, class_name) in _CREDENTIALED_PROVIDERS.items():
        if is_configured(name, settings):
            providers[name] = getattr(import_module(module), class_name)
    for name, (flag, module, class_name) in _FLAGGED_PROVIDERS.items():
        if getattr(settings, flag):
            providers[name] = getattr(import_module(module), class_name)
    return providers


_storage_provider = None


def get_storage_provider():
    """Get the configured storage provider (singleton)."""
    global _storage_provider
    if _storage_provider is None:
        from app.core.config import get_settings

        settings = get_settings()
        if settings.storage_provider == "local":
            from app.providers.local_storage import LocalStorageProvider

            _storage_provider = LocalStorageProvider()
        elif settings.storage_provider == "s3":
            from app.providers.s3_storage import S3StorageProvider

            _storage_provider = S3StorageProvider()
        else:
            raise NotImplementedError(
                f"Storage provider '{settings.storage_provider}' is not yet implemented. "
                "Supported: 'local', 's3'"
            )
    return _storage_provider


__all__ = [
    "BankProvider",
    "AccountData",
    "TransactionData",
    "ConnectionData",
    "ConnectTokenData",
    "HoldingData",
    "InstitutionData",
    "InstitutionListData",
    "ProviderUserActionRequired",
    "RefreshOutcome",
    "SessionExpiredError",
    "register_provider",
    "get_provider",
    "all_known_providers",
    "get_storage_provider",
]
