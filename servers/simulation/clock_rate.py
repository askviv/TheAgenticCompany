"""Read the active guest-kernel virtual-time rate for native-duration scaling."""

from pathlib import Path


class InvalidKernelVirtualTimeRate(ValueError):
    """The kernel exposes a rate that cannot be used for duration scaling."""


def kernel_virtual_time_rate(*, require_kernel: bool = False) -> int:
    """Return the positive kernel rate, or 1 when an unpatched host is allowed."""
    try:
        value = Path('/sys/kernel/virtual_time_rate').read_text(encoding='ascii').strip()
    except FileNotFoundError:
        if require_kernel:
            raise
        return 1
    rate = int(value)
    if rate < 1:
        raise InvalidKernelVirtualTimeRate('guest kernel virtual-time rate must be positive')
    return rate
