# EV Charging (Stripe)

OCPP central system for one charger, with pay-per-kWh card payment through Stripe
Checkout: the driver plugs in, scans the QR code, pays on Stripe's page, charges, and is
charged only for the energy used. See the repository's `docs/payments/stripe.md`.

## Ports

| Port | What |
|---|---|
| 9000 | OCPP. Set the charger's backend URL to `ws://<this host's LAN address>:9000/` |
| 8081 | Pay pages, `/admin` and `/api/status` on the LAN (and on `trusted_networks`) |

The public side (pay pages and `/webhooks/stripe`) is meant to go through the
Cloudflared add-on: add a host to its `additional_hosts`, service
`http://<this add-on's hostname>:8080`. `/admin` and `/api` refuse anything that came
through the tunnel.

## Options

| Option | Meaning |
|---|---|
| `stripe_secret_key` | Restricted key (`rk_test_…` / `rk_live_…`): Checkout Sessions and PaymentIntents, write |
| `stripe_webhook_secret` | `whsec_…` of the endpoint `https://<public host>/webhooks/stripe` |
| `currency`, `price_per_kwh`, `session_fee` | e.g. `EUR`, `0.40`, `0` |
| `hold_options` | Amounts the driver can hold, e.g. `50` or `20, 50` |
| `public_url` | `https://<public host>`, where Stripe sends the driver back to |
| `socket_name`, `charge_point_id` | Socket shown to drivers (`CP1`), and the charger's OCPP id (often its serial number) |
| `ocpp_allow_ws` | Plain `ws://` from the LAN (most home chargers) |
| `ocpp_password` | Optional HTTP Basic password for the charger |
| `admin_password` | Enables `/admin` (any user name) |
| `api_token` | Enables `/api/status` for the Home Assistant integration |
| `timezone` | Local time for tariffs and the admin pages |
| `trusted_networks` | Extra networks treated as the LAN, e.g. Tailscale `100.64.0.0/10` |
| `brand_*`, `footer_text`, `page_note`, `support_contact` | What drivers see |
| `statement_name` | Suffix on the card statement |
| `receipts_*` | Optional own e-mail receipt (Stripe sends one anyway in live mode) |

The session database is `/data/ev.sqlite3`, included in Home Assistant backups.
