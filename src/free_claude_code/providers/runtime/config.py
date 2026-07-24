"""Provider configuration construction from neutral catalog metadata."""

from free_claude_code.application.errors import ApplicationUnavailableError
from free_claude_code.config.provider_catalog import ProviderDescriptor
from free_claude_code.config.settings import Settings
from free_claude_code.providers.base import ProviderConfig
from free_claude_code.providers.key_pool import parse_key_list


def string_setting(settings: Settings, attr_name: str | None, default: str = "") -> str:
    """Return a string-valued settings attribute, ignoring non-string mocks."""
    if attr_name is None:
        return default
    value = getattr(settings, attr_name, default)
    return value if isinstance(value, str) else default


def provider_credential(descriptor: ProviderDescriptor, settings: Settings) -> str:
    """Return the configured credential for a provider descriptor.

    The singular credential wins; when it is empty and the descriptor declares
    a plural key-pool attribute, the first configured plural key is used so
    that plural-only configuration is accepted downstream.
    """
    if descriptor.static_credential is not None:
        return descriptor.static_credential
    if descriptor.credential_attr:
        value = string_setting(settings, descriptor.credential_attr)
        if value.strip():
            return value
    if descriptor.credential_attr_plural:
        keys = parse_key_list(
            string_setting(settings, descriptor.credential_attr_plural)
        )
        if keys:
            return keys[0]
    return ""


def has_provider_configuration(
    descriptor: ProviderDescriptor, settings: Settings
) -> bool:
    """Return whether all provider-defining settings are present.

    For providers with a plural key-pool attribute, the credential requirement
    is satisfied by EITHER the singular key OR a non-empty plural list.
    """
    if descriptor.credential_attr_plural is not None:
        singular = (
            string_setting(settings, descriptor.credential_attr)
            if descriptor.credential_attr
            else ""
        )
        if singular.strip():
            return True
        return bool(
            parse_key_list(string_setting(settings, descriptor.credential_attr_plural))
        )
    attrs = descriptor.configuration_attrs()
    if attrs:
        return all(string_setting(settings, attr).strip() for attr in attrs)
    return descriptor.static_credential is not None


def require_provider_credential(
    descriptor: ProviderDescriptor, credential: str
) -> None:
    """Raise a user-facing configuration error when a required key is missing."""
    if descriptor.credential_env is None:
        return
    if credential and credential.strip():
        return
    message = f"{descriptor.credential_env} is not set. Add it to your .env file."
    if descriptor.credential_url:
        message = f"{message} Get a key at {descriptor.credential_url}"
    raise ApplicationUnavailableError(message)


def build_provider_config(
    descriptor: ProviderDescriptor, settings: Settings
) -> ProviderConfig:
    """Build shared provider configuration for one provider descriptor."""
    credential = provider_credential(descriptor, settings)
    require_provider_credential(descriptor, credential)
    base_url = string_setting(
        settings, descriptor.base_url_attr, descriptor.default_base_url or ""
    )
    resolved_base_url = base_url or descriptor.default_base_url
    if not resolved_base_url:
        raise AssertionError(
            f"Provider {descriptor.provider_id!r} has no configured base URL."
        )
    proxy = string_setting(settings, descriptor.proxy_attr)
    return ProviderConfig(
        api_key=credential,
        base_url=resolved_base_url,
        rate_limit=settings.provider_rate_limit,
        rate_window=settings.provider_rate_window,
        max_concurrency=settings.provider_max_concurrency,
        http_read_timeout=settings.http_read_timeout,
        http_write_timeout=settings.http_write_timeout,
        http_connect_timeout=settings.http_connect_timeout,
        proxy=proxy,
        log_raw_sse_events=settings.log_raw_sse_events,
        log_api_error_tracebacks=settings.log_api_error_tracebacks,
    )
