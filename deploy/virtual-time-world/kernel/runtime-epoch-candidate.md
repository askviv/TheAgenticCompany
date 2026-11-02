# Durable boot epoch candidate

`linux-6.12.115-runtime-epoch.patch` layers after the fixed-rate patch and the
runtime-rate patch. It adds four early boot parameters:

```text
virtual_time_epoch_sec=<Unix seconds>
virtual_time_epoch_nsec=<0..999999999>
virtual_time_host_epoch_sec=<Unix seconds>
virtual_time_host_epoch_nsec=<0..999999999>
```

The guest nanosecond parameter is optional and defaults to zero; it requires
the guest seconds parameter. The host anchor parameters must be supplied
together and require the guest epoch parameters. Each timestamp is validated
against the kernel's `CLOCK_REALTIME` range. Invalid or partial parameter sets
fail during boot rather than silently falling back to a stale clock.

The host controller sends a paired durable anchor: the guest epoch and the host
realtime at which that guest epoch was sampled. At `timekeeping_init()`, the
kernel reads the KVM persistent host wall clock and computes
`guest_epoch + (host_wall_at_init - host_anchor) * boot_rate`, carrying
fractional nanoseconds and rejecting host rollback or arithmetic overflow.
This accounts for host launch and firmware delay between domain creation and
timekeeper initialization. For the initial boot, `initialize` uses the chosen
guest epoch and host realtime captured together. If the rate varies while the
VM is powered off, the controller must integrate that rate schedule; this
candidate currently uses the persisted selected rate for that interval.

For a manual static epoch, guest epoch parameters may be supplied without host
anchor parameters; the kernel installs that timestamp exactly at initialization.

`timekeeping_init()` still obtains `boot_offset` from `local_clock()`, then
replaces only the realtime `wall_time` with the supplied epoch. Existing
`wall_to_mono = boot_offset - wall_time` setup remains unchanged, preserving
Linux's invariant that realtime plus the wall-to-monotonic offset equals this
boot's monotonic base. `CLOCK_MONOTONIC` therefore starts for the new boot as
usual; the previous boot's monotonic value is not persisted. The override does
not claim a hardware persistent clock exists and leaves the existing
`persistent_clock_exists` detection intact.

For this parameter to remain authoritative, build with
`CONFIG_RTC_HCTOSYS=n`. Otherwise RTC class registration can later reset wall
time from the emulated RTC after `timekeeping_init()`.

## Why not rely on RTC offset alone

Libvirt documents `clock offset='variable'` with a seconds-only adjustment
that it retains across guest RTC adjustments, and `offset='absolute'` with an
epoch start value. QEMU's RTC base can likewise be set to a datetime. Those
control the emulated RTC. On x86 KVM, Linux installs
`x86_platform.get_wallclock = kvm_get_wallclock()`; `timekeeping_init()` calls
`read_persistent_wall_and_boot_offset()`, which reaches that KVM pvclock
wallclock path before RTC class registration. The QEMU RTC offset therefore is
not a reliable way to seed the initial KVM persistent wallclock. The explicit
kernel epoch overrides that input. Disable RTC_HCTOSYS to prevent a later RTC
read from replacing it.

The combined patches have been built and booted in the empty Ubuntu guest.
The epoch patch does not persist data itself and cannot
protect against loss/corruption of the host controller's anchor. Host real-time
clock jumps, abrupt host power loss before the last guest epoch is persisted,
and system suspend remain outside the guarantee. Boot at rate one and let the
host controller reconcile the persisted rate and epoch before starting company
applications, as described in the deployment guide.
