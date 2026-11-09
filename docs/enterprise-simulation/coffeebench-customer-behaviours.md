# CoffeeBench customer behaviours

The company is `roaster_A`. Five other businesses can buy from it. The two retailers are the most natural repeat buyers of roasted coffee. The other roaster and the two farmers can also buy through the marketplace.

For company sales, CoffeeBench waits for ERPNext to submit the invoice and record a successful send to the mapped buyer before that buyer can pay. The table distinguishes current benchmark mechanics from proposed scenarios for Viv.

| Behaviour | What the customer does | What Viv can observe or do | Status |
| --- | --- | --- | --- |
| Prompt payer | Pays when the invoice arrives and funds are available. | Reconcile the receipt without sending a reminder. | Possible through the native buyer agent |
| Friday processing | Reviews invoices on Fridays in its local time. | Infer the weekly rhythm and send before the next review. | Proposed |
| Morning processing | Pays invoices received before 10 a.m. that day and queues later arrivals. | Learn the preferred send time from earlier payments. | Proposed |
| Batch cutoff | Pays in a weekly batch. Invoices after the cutoff roll into the next batch. | Infer the cutoff and avoid unnecessary reminders. | Proposed |
| Reminder needed | Pays after a relevant reminder on the original thread. | Learn which customers need one follow-up and when. | Proposed |
| Missing purchase order reference | Holds an invoice until it carries the correct PO reference. | Ask what is blocking payment, correct the reference and resend. | Proposed |
| Receiving mismatch | Holds payment when delivery quantity or condition differs from the invoice. | Check receiving evidence and resolve the discrepancy or arrange a credit. | Proposed. Native shipment delays, losses and returns provide starting events |
| Wrong billing recipient | Leaves an invoice waiting until it reaches the accounts payable contact. | Confirm the billing contact and resend to the right person. | Proposed |
| Partial payment | Pays only the undisputed amount. | Apply the receipt, track the remainder and follow up on the disputed amount. | Proposed. Native `pay_invoice` settles the full outstanding balance |
| Temporary cash shortage | Defers payment until cash is available. | Spot the delay, ask for a realistic date and revisit the account. | Native insufficient-cash handling. A scripted recovery pattern is proposed |
| Requests longer credit terms | Asks for more time to pay on a new order, for example net 60. | Check the account and exposure, then approve, decline or request approval under the company policy. | Native offer terms. A scripted request is proposed |
| Requests a higher credit limit | Asks to place an order beyond the existing credit limit. | Check total exposure and ask for a deposit, smaller order or approval. | Proposed |
| Financial distress | Pays progressively later or requests longer terms before a new order. | Combine payment history with a credible risk signal and decide whether to offer further credit. | Proposed |
| Bankruptcy | Stops trading when the benchmark declares the business bankrupt. Outstanding receivables can become bad debt. | Treat confirmed insolvency as a reason to stop extending new credit and review existing exposure. | Native bankruptcy and write-off. A scripted warning and credit decision are proposed |

CoffeeBench offers already carry `payment_terms_days`. A buyer can ask for net 60 by making an offer with 60 days. Accepting the offer sets that term on the deal. That models trade credit on a specific order. A standalone credit application, shared credit limit and approval workflow would need their own scenario.

## Proposed switches

Add an operator-only setting under `company.background.coffee`, keyed by counterparty ID. Each behaviour can have an `enabled` switch and parameters such as the time zone, review day, cutoff or requested credit term. Capture the settings at run start so an experiment can be replayed. Keep the rules private to the simulator. Viv sees normal business evidence such as offers, invoices, messages and payments.

Start with timing, a missing PO reference and a distressed buyer requesting net 60. Run several orders per customer and measure whether Viv changes its approach, reduces days to payment and avoids new credit exposure after warning signs appear. For the credit case, send the request and warning before the seller accepts the order so Viv has a decision to make. Actual bankruptcy remains a separate event that can reveal the cost of earlier credit decisions.

These switches describe a proposed configuration. The current benchmark uses native buyer agents and has no operator switches for these scripted behaviours.
