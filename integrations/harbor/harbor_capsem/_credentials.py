"""Automatic host credential brokering for `CapsemEnvironment.exec()`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_CREDENTIAL_REF_PREFIX = "credential:blake3:"
_RUNTIME_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_RUNTIME_CA_ENV: dict[str, str] = {
    "SSL_CERT_FILE": _RUNTIME_CA_BUNDLE,
    "REQUESTS_CA_BUNDLE": _RUNTIME_CA_BUNDLE,
    "PIP_CERT": _RUNTIME_CA_BUNDLE,
    "CURL_CA_BUNDLE": _RUNTIME_CA_BUNDLE,
    "GIT_SSL_CAINFO": _RUNTIME_CA_BUNDLE,
    "NODE_EXTRA_CA_CERTS": _RUNTIME_CA_BUNDLE,
    "UV_NATIVE_TLS": "1",
    "TMPDIR": "/var/tmp",
}
_PROVIDER_KEY_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ANTHROPIC_API_KEY": ("anthropic", ("ANTHROPIC_BASE_URL",)),
    "OPENAI_API_KEY": ("openai", ("OPENAI_BASE_URL",)),
    "GEMINI_API_KEY": ("google", ("GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL")),
    "GOOGLE_API_KEY": ("google", ("GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL")),
}


def coerce_bool_flag(value: Any, *, default: bool) -> bool:
    """Coerce a boolean or `--ek key=true|false` CLI string into a `bool`."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if not norm:
            return default
        if norm in ("1", "true", "yes", "on"):
            return True
        if norm in ("0", "false", "no", "off"):
            return False
    raise ValueError(f"Invalid boolean option value: {value!r}")


def should_broker_credential(
    provider: str,
    raw_value: str,
    env: Mapping[str, str],
    base_url_keys: tuple[str, ...] = (),
) -> bool:
    """Return True when `raw_value` should be registered with the Capsem credential broker."""
    del provider, env, base_url_keys
    if not isinstance(raw_value, str) or not (stripped := raw_value.strip()):
        return False
    return not stripped.startswith(_CREDENTIAL_REF_PREFIX)


async def _inject_credential(controller: Any, provider: str, raw_value: str) -> str:
    inject_fn = getattr(controller, "inject_credential", None)
    if callable(inject_fn):
        resp = await inject_fn(provider, raw_value, storage="memory")
    else:
        hv = getattr(controller, "hypervisor", None) or getattr(controller, "_hypervisor", None)
        creds = getattr(hv, "credentials", None) if hv is not None else None
        if creds is None or not callable(getattr(creds, "inject", None)):
            raise RuntimeError("Capsem controller does not expose hypervisor.credentials.inject")
        resp = await creds.inject(provider, raw_value, storage="memory")
    ref = getattr(resp, "credential_ref", resp)
    if not isinstance(ref, str) or not ref.startswith(_CREDENTIAL_REF_PREFIX):
        raise ValueError(f"Invalid credential reference returned for {provider}: {ref!r}")
    return ref


async def broker_env_credentials(
    controller: Any,
    env: Mapping[str, str],
    *,
    enabled: bool = True,
    cache: dict[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """Rewrite raw provider API keys in `env` into `credential:blake3:<hex>` references."""
    out = dict(env)
    if not enabled:
        return out
    for env_key, (provider, base_url_keys) in _PROVIDER_KEY_SPECS.items():
        raw_value = out.get(env_key)
        if raw_value is None or not should_broker_credential(
            provider, raw_value, out, base_url_keys
        ):
            continue
        cache_key = (provider, raw_value)
        if cache is not None and cache_key in cache:
            out[env_key] = cache[cache_key]
            continue
        ref = await _inject_credential(controller, provider, raw_value)
        if cache is not None:
            cache[cache_key] = ref
        out[env_key] = ref
    return out


__all__ = [
    "_CREDENTIAL_REF_PREFIX",
    "_PROVIDER_KEY_SPECS",
    "_RUNTIME_CA_ENV",
    "broker_env_credentials",
    "coerce_bool_flag",
    "should_broker_credential",
]
