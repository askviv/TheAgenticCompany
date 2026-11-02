# Linux guest time patches

The Linux kernel patch is GPL-2.0-only, matching the modified kernel code;
see `COPYING.GPL-2.0`. The upstream source archive is downloaded by the builder
and is not vendored into this repository.

This directory contains a candidate patch against the upstream Linux 6.12.115
longterm release. It is intended for an x86_64 KVM guest configured with high
resolution timers and NO_HZ/one-shot clockevents. The patch is limited to
generic timekeeping, clockevent conversion and vDSO overflow bounds.

Apply `linux-6.12.115-virtual-time.patch` to an unmodified 6.12.115 source
tree. The production launch sequence boots with `virtual_time=1` and changes
the rate through sysfs after the KVM clocksource is active. Passing an accelerated
rate during early boot scales the initial jiffies clocksource twice and must
not be used. At runtime, `N=24` makes one guest second elapse per 1/24 host second.
The fixed-rate patch alone is boot-only. The builder also applies the separate runtime-rate and
durable-epoch patches, in that order. These add the sysfs rate control and
host-supplied boot epoch described in their candidate documents.

## Clock behavior

The timekeeper keeps the hardware clocksource as its counter and scales its
cycle-to-nanosecond conversion. This applies to realtime, monotonic, boot-time,
raw monotonic and coarse timekeeper reads. The generic timekeeper update path
publishes the changed multiplier through the x86 vDSO data and KVM pvclock
snapshot. The x86 KVM vDSO therefore reads the scaled time without a separate
syscall-only override. The guest's normal VFS timestamp path uses realtime, so
inode timestamps on guest-local filesystems follow guest time.

Generic one-shot clockevent programming converts guest-time deadlines to host
intervals by dividing by `N`. This covers high-resolution timers, sleeps,
POSIX timers, timerfd and one-shot tick deadlines. The tick broadcast KTIME
device in this kernel is an hrtimer proxy: it keeps the logical guest deadline,
and the hrtimer's eventual hardware deadline is converted by the generic path.
The patch does not scale a device that implements KTIME deadlines directly in
hardware. On x86 KVM, the target hardware event devices are the LAPIC TSC-deadline/count-down one-shot devices, with HPET one-shot available when enabled. Their drivers use the generic cycle-based one-shot path; retain that configuration when building the guest.

The rate must be positive and fit the NTP fixed-point interval arithmetic:

```text
N <= S64_MAX / ((u64)NTP_INTERVAL_LENGTH << NTP_SCALE_SHIFT)
```

The selected clocksource must also have enough conversion shift to represent
`clocksource.mult * N` in its 32-bit multiplier while leaving headroom for
timekeeper adjustment. The implementation reduces the conversion shift when
needed; if the source shift is insufficient, the kernel fails clearly during
timekeeper setup instead of silently booting at 1x. The multiplier-derived
`max_cycles` and NO_HZ deferment are also reduced to safe bounds, including the
vDSO bounds, so the accelerated multiplier does not turn a long idle/read into
an overflowing multiply. The NO_HZ deferment conversion uses the original
physical clocksource multiplier and therefore errs conservatively (it may
request an earlier tick than the tightest accelerated bound). The patch adjusts the NTP tick baseline for the selected rate so timekeeper
frequency arithmetic retains the multiplier. It does not make NTP a valid
source of guest time: chrony/ntpd, other `clock_settime` writers, and QEMU
guest-agent clock synchronization must be disabled. Such writers can step the
guest wall clock or discipline it toward host UTC.

One-shot clockevent programming is the path scaled by this patch. Build with
`CONFIG_HIGH_RES_TIMERS=y` and `CONFIG_NO_HZ_COMMON=y`, and retain one-shot
tick operation after early boot. Hardware periodic clockevent mode is not
scaled. Direct KTIME hardware devices are not scaled either; the in-tree x86
KVM clockevent path uses ordinary one-shot hardware devices, while its KTIME
tick-broadcast proxy is backed by an hrtimer.

## Boundaries and review gaps

- Guest CPU work is not sped up. CPU-time clocks and x86 `sched_clock()` are
  not virtualized by this patch; short busy-waits such as `udelay()` still
  consume host execution time.
- Suspend/resume is not covered. The guest must remain running while using
  the accelerated clock; persisted-clock suspend accounting would need to be
  reconciled with the selected rate.
- Use guest-local block filesystems for container data. A host-mounted
  virtiofs or bind-mounted host directory supplies timestamps from the host
  filesystem and cannot satisfy the guest timestamp contract.
- Traffic and services outside the guest retain their own clocks. Network
  delivery time, mail-provider timestamps and remote inference duration are
  not accelerated.
- The combined patches have been built and booted in an empty Ubuntu guest.
  Runtime 1×/24× changes and clock reads have been observed there. Company
  migration and application compatibility remain separate deployment work.
