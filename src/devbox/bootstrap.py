"""Render first-boot configuration without shell-interpolating user values."""

from importlib import resources
from textwrap import indent


def render_cloud_init(swap_size_mib: int) -> str:
    if type(swap_size_mib) is not int or swap_size_mib < 0:
        raise ValueError("swap_size_mib must be a non-negative whole number")
    package = resources.files("devbox")
    template = package.joinpath("cloud-init.yaml").read_text()
    helper = package.joinpath("setup_swap.py").read_text()
    return template.replace("      __DEVBOX_SWAP_HELPER__", indent(helper.rstrip(), "      ")).replace(
        "__DEVBOX_SWAP_SIZE_MIB__", str(swap_size_mib)
    )
