# Runtime-rate candidate patch

`linux-6.12.115-runtime-rate.patch` is a separate, unbuilt candidate layered
on `linux-6.12.115-virtual-time.patch`. It does not modify the fixed-rate
patch. Apply it after the fixed-rate patch to the same 6.12.115 source tree.

It adds root-writable `/sys/kernel/virtual_time_rate` control (readable by all;
write additionally requires `CAP_SYS_TIME`). A successful write commits a
new positive integer rate using `stop_machine()`, while preserving visible
clock values at the boundary, then synchronously notifies and rearms timers.
Invalid numeric/fixed-point/active-clocksource rates return an error and leave
the old rate in place. Clocksource selection also declines a new source that
cannot represent the active rate, instead of reaching the fixed patch's
setup panic.

The candidate pairs the monotonic sample and rate under `tk_core.seq`; after
programming a physical event it checks `clock_was_set_seq` and retries if a
rate/clock-set commit crossed the programming window. It re-arms hrtimers on
every online CPU and local NO_HZ one-shot events, then reprograms the one-shot
broadcast device under its lock. CPU read locks cover each synchronous rearm
phase. Minimum-delta fallback events also publish the corresponding guest
absolute deadline (`host min-delta × rate`) using a paired time/rate sample; the
outer sequence check retries if a rate or clock-set update crossed programming.
It keeps the in-tree KTIME hrtimer broadcast expiry in guest time so the eventual
hardware event is converted once by the generic path. A rate write returns
`-EOPNOTSUPP` while an online local tick device is in periodic mode, or an
active broadcast device is periodic, or when a direct KTIME hardware event
device is selected; valid one-shot, one-shot-stopped, and shutdown-for-broadcast
idle states are accepted. Rearming uses native `tick_program_event()` so stopped
local devices restart only for finite pending deadlines; stopped broadcasts are
restarted only when their next deadline is finite. A no-event `KTIME_MAX` state
remains stopped.
Unsupported event paths leave the rate unchanged.

The combined patches have been built and booted in an empty Ubuntu guest;
runtime changes between 1× and 24× have been observed there. Timer
reprogramming after the atomic timekeeper commit is
synchronous, but a deadline that becomes due during that rearm window can be
late by the stop-machine release and IPI/reprogram latency. NTP interval phase
state is reinitialized by the existing `tk_setup_internals()` helper during a
rate change; the time itself does not step, and `TK_CLEAR_NTP` is not requested.
