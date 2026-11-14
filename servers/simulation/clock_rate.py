"""Read the active guest-kernel virtual-time rate for native-duration scaling."""

from datetime import datetime, timezone
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


def simulation_message_footer() -> str:
    """Describe the guest clock when an outbound message is prepared."""
    rate = kernel_virtual_time_rate()
    simulated_time = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    return (
        f"This is a simulated environment. The clock runs at {rate}× real time. "
        f"The current simulated time is {simulated_time}."
    )
