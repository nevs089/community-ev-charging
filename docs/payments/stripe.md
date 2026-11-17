# Stripe

Stripe Checkout with `capture_method=manual`: the driver pays on Stripe's own page,
Stripe holds the amount on the card, and when the charge ends the service captures
only the cost of the energy. Stripe releases the rest of the hold.

Card payments include Apple Pay and Google Pay. The driver needs no account.

## How a charge works

| Driver | Service | Stripe |
|---|---|---|
| Plugs in, opens the socket's page, picks the hold, presses *Continue to payment* | Creates a Checkout Session for the hold (`POST /pay/api/checkout`), redirects | Checkout page, open for 30 minutes |
| Pays | | PaymentIntent in `requires_capture`: the hold |
| Comes back to `/pay/session/<id>` | Reads the Checkout Session; hold in place → `AUTHORISED` → `RemoteStartTransaction` | |
| | The webhook `checkout.session.completed` does the same, whichever comes first | |
| Charges, then unplugs or presses Stop | Captures the cost with `amount_to_capture` | Captures, releases the rest, sends the receipt |
| Car never charges | Cancels the PaymentIntent | Hold released |

A second driver who pays for a socket that someone else has just taken has their hold
cancelled at once (`VOIDED`, nothing charged). A charger that refuses the start gets the
same.

## Settings

```
PAYMENT_PROVIDER=stripe
STRIPE_SECRET_KEY=rk_live_...        # a restricted key, see below
STRIPE_WEBHOOK_SECRET=whsec_...
CURRENCY=EUR
PUBLIC_URL=https://ev.example.org    # where Stripe sends the driver back to
# STRIPE_API_VERSION=2024-06-20      # pinned; change only after checking the changelog
# STATEMENT_NAME=EV CHARGING         # suffix on the card statement
```

### The key

Use a **restricted key** (Developers → API keys → Create restricted key) with:

| Resource | Permission |
|---|---|
| Checkout Sessions | Write |
| PaymentIntents | Write |

Everything else: None. A secret key (`sk_...`) also works, but can do anything on the account.

### The webhook

Developers → Webhooks → Add endpoint: `https://<your host>/webhooks/stripe`, events:

- `checkout.session.completed`
- `checkout.session.expired`
- `payment_intent.canceled`
- `payment_intent.succeeded`
- `charge.refunded`, `charge.dispute.created` (logged against the session only)

Copy the signing secret into `STRIPE_WEBHOOK_SECRET`. The service checks the
`Stripe-Signature` header (HMAC-SHA256, 5-minute tolerance) itself; no SDK is used.
The cloudflared ingress must let `/webhooks/stripe` through (see
`deploy/cloudflared/config.yml.example`).

### Receipts

Stripe sends the receipt itself, with the amount actually captured, when *Settings →
Customer emails → Successful payments* is on (live mode only; test mode sends none).
The service's own SMTP receipt (`SMTP_*`) is optional with Stripe.

## Testing

Use test-mode keys (`sk_test_`/`rk_test_`) and card `4242 4242 4242 4242`, any future
date, any CVC. With the simulator ([../ocpp.md](../ocpp.md)) the whole flow runs without
a car: the PaymentIntent shows as *Uncaptured* in the dashboard after paying, then as
*Succeeded* with the partial amount once the simulated charge stops.

`stripe listen --forward-to localhost:8081/webhooks/stripe` (Stripe CLI) delivers
webhooks to a machine without a public address while testing.

## Limits

- A card hold lasts **7 days**. A charge longer than that loses its hold
  (`payment_intent.canceled`, session `VOIDED`).
- Checkout pages expire after 30 minutes; an unpaid session becomes `ABANDONED`.
- Refunds and disputes are handled in the Stripe dashboard.
