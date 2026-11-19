"""Compatibility alias: the Hetzner code lives in `devbox.providers.hetzner`."""

from devbox.providers.hetzner import *  # noqa: F401,F403
from devbox.providers.hetzner import (  # noqa: F401
    CONTROLLER_LABEL,
    MANAGED_LABEL,
    Hcloud,
    HetznerProvider,
    _price,
    _same_key,
    build_offers,
    filter_offers,
    firewall_rules,
    image_info,
    same_rules,
    server_info,
)
