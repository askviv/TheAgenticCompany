# Company business clock

The coordinator owns one persistent business clock. CoffeeBench, E-CommerceBench,
company events and experiment deadlines consume its company time. The dashboard
shows this same time and exposes a speed multiplier: 24× means one company day
per real hour. A rate change preserves the current instant and pending events.

Clock state, rate and idempotency receipts are stored in `company-clock.json`.
The publisher writes the current instant to the modification time of its private
follow file. This is an internal coordinator publication mechanism; applications
and infrastructure do not mount it or intercept system clocks. On restart the
clock accrues elapsed real time at its saved rate. Dispatch delays do not stop
the independent publisher or discard pending events.

`POST /company/clock/speed` accepts a positive `speed` and an `Idempotency-Key`.
Existing controller authorization applies. The dashboard proxies this operation.

Generated experiment email labels its event's company time in the body and, for
SMTP delivery, `X-TAC-Company-Time`. Native delivery and audit timestamps stay real.
ERPNext business dates remain the explicit source/approval dates; commitments are
never replaced by the current clock, and missing dates are not invented.

Application clocks, database expiry, certificates, email transport, browser
sessions and network waits use real time. The unread issue handoff remains five
real minutes. Native benchmark criteria and their scoring scales are unchanged.
Background provider switches retain their current settings; changing clock speed
does not start an experiment or enable a stopped provider.
