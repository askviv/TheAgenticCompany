# CoffeeBench customer behaviours

The company is `roaster_A`. Five other businesses can buy from it. The two retailers are the most natural repeat buyers of roasted coffee. The other roaster and the two farmers can also buy through the marketplace.

For company sales, CoffeeBench waits for ERPNext to submit the invoice and record a successful send to the mapped buyer before that buyer can pay. The table distinguishes current benchmark mechanics from proposed scenarios for Viv.

| Behaviour | What the customer does | What Viv can observe or do | Status |
| --- | --- | --- | --- |
| Prompt payer | Pays when the invoice arrives and funds are available. | Reconcile the receipt without sending a reminder. | Possible through the native buyer agent |
| Friday processing | Can pay on Fridays on the simulated UTC clock. | Infer the weekly rhythm and send before the next review. | Operator switch |
| Morning processing | Can pay before 10 a.m. on the simulated UTC clock. | Learn when payments tend to happen and send invoices early. | Operator switch |
| Requests order financing | Asks for credit to fund a coffee order. | Receives the request with the ERPNext order reference and can create an application in Lending. | Operator switch |
| Batch cutoff | Processes payments on Fridays from 10 a.m. simulated UTC. Invoices first observed at or after the cutoff roll into the next Friday batch. | Infer the cutoff and avoid unnecessary reminders. | Operator switch |
| Reminder needed | Pays after a delivered reply on the invoice thread asks for payment. | Follow up on the invoice thread. | Operator switch |
| Missing purchase order reference | Holds payment until the requested PO is on a delivered invoice. | Correct the PO and resend after the finance handoff. | Operator switch |
| Receiving mismatch | Receives fewer units than ordered and disputes the value of the missing units. | Review the finance handoff and submit a linked credit note. | Operator switch |
| Wrong billing recipient | The first invoice delivery is routed to the internal finance reviewer instead of the buyer’s accounts payable mailbox. | Resend to the accounts payable address provided in the finance handoff. | Operator switch |
| Partial payment | Pays half of the balance and disputes the remainder. | Reconcile the separate receipt and resolve the remainder with a linked credit note. | Operator switch |
| Temporary cash shortage | Defers payment until cash is available. | Spot the delay, ask for a realistic date and revisit the account. | Native insufficient-cash handling. A scripted recovery pattern is proposed |
| Requests longer credit terms | Asks for more time to pay on a new order, for example net 60. | Check the account and exposure, then approve, decline or request approval under the company policy. | Native offer terms. A scripted request is proposed |
| Requests a higher credit limit | Asks to exceed the existing ERPNext credit limit. An unconfigured limit starts at half the first qualifying order. | Review the request through Finance and submit the order if approved. | Operator switch |
| Financial distress | Requests longer terms and increasingly defers payments as prior invoices accumulate, in seven-day steps up to 28 days. | Review the terms request and use the late-payment history when deciding whether to extend credit. | Operator switch |
| Bankruptcy | Stops trading when the benchmark declares the business bankrupt. Outstanding receivables can become bad debt. | Treat confirmed insolvency as a reason to stop extending new credit and review existing exposure. | Native bankruptcy and write-off. A scripted warning and credit decision are proposed |

CoffeeBench offers already carry `payment_terms_days`. A buyer can ask for net 60 by making an offer with 60 days. Accepting the offer sets that term on the deal. That models trade credit on a specific order. A standalone credit application, shared credit limit and approval workflow would need their own scenario.

## Customer settings

Open **Experiments → Background simulations → CoffeeBench → Behaviour settings**.
Switch on a behaviour from the settings page. The Friday batch schedule takes precedence if the same customer also has the morning payment window. CoffeeBench chooses the
counterparties privately and keeps those choices in its checkpoint. The settings
page and API show only which behaviours are enabled. Changes apply on the next
advance. A payment
still requires a submitted invoice sent to the buyer and sufficient buyer cash.

When a selected customer makes an offer that CoffeeBench imports as a new
Sales Order draft, the finance handoff asks Viv to handle the customer's credit
request. It includes the customer, order reference and requested amount.
Viv can use that request to create an application in Lending.

The company uses three simulated Lending products with 12 monthly payments.
Amounts are in the Lending company's base currency (GBP on the Mini).

| Product | Maximum | Annual interest | Payment history |
| --- | ---: | ---: | --- |
| Prime | £10,000 | 8% | At least one recorded payment and no late payments |
| Near prime | £5,000 | 14% | No recorded payments, or qualifies for prime |
| Subprime | £2,500 | 22% | Available for all three payment histories, including a recorded late payment |

Viv creates and submits the Loan Application with the **Coffee Sales Order**
link. On the next finance reconciliation, CoffeeBench checks the linked
request, product, amount and term, records the decision in Lending, and queues
an email back to Viv. An approval hands the order back to Viv to continue.
