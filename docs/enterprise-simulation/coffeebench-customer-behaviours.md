# CoffeeBench customer behaviours

The company is `roaster_A`. Five other businesses can buy from it. The two retailers are the most natural repeat buyers of roasted coffee. The other roaster and the two farmers can also buy through the marketplace.

For company sales, CoffeeBench waits for ERPNext to submit the invoice and record a successful send to the mapped buyer before that buyer can pay. The table distinguishes current benchmark mechanics from proposed scenarios for Viv.

| Behaviour | What the customer does | What Viv can observe or do | Status |
| --- | --- | --- | --- |
| Prompt payer | Pays when the invoice arrives and funds are available. | Reconcile the receipt without sending a reminder. | Possible through the native buyer agent |
| Friday processing | Can pay on Fridays on the simulated UTC clock. | Infer the weekly rhythm and send before the next review. | Operator switch |
| Morning processing | Can pay before 10 a.m. on the simulated UTC clock. | Learn when payments tend to happen and send invoices early. | Operator switch |
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

## Customer settings

Open **Experiments → Background simulations → CoffeeBench → Behaviour settings**.
Switch on Friday payments or payments before 10 a.m. CoffeeBench chooses the
counterparties privately and keeps those choices in its checkpoint. The settings
page and API show only which behaviours are enabled. Changes apply on the next
advance. A payment
still requires a submitted invoice sent to the buyer and sufficient buyer cash.

The proposed scenarios in the table need their own simulation rules. For a
financing scenario, a buyer could request a working-capital loan to fund an
order. The application would go to Lending for approval. A disbursement could
fund the sale, and the buyer would repay the lender according to the loan
product's rate and schedule. The credit decision would need to happen before
funds are released.
