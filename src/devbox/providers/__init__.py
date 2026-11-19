"""Provider registry. `get(name, cfg)` builds a provider; `capabilities(name)` needs no credentials."""

from devbox import DevboxError
from devbox.providers.base import Access, Capabilities, Provider, require  # noqa: F401
from devbox.providers.hetzner import HetznerProvider
from devbox.providers.ssh import SshProvider

DEFAULT = "hetzner"

_REGISTRY: dict[str, type] = {HetznerProvider.name: HetznerProvider, SshProvider.name: SshProvider}


def register(cls: type) -> type:
    """Make a provider class available under `cls.name` (also used by tests)."""
    _REGISTRY[cls.name] = cls
    return cls


def unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


def names() -> list[str]:
    return sorted(_REGISTRY)


def provider_class(name: str) -> type:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise DevboxError(f"unknown provider '{name}'; known: {', '.join(names())}")


def get(name: str, cfg) -> Provider:
    return provider_class(name).from_config(cfg)


def capabilities(name: str) -> Capabilities:
    return provider_class(name).capabilities
