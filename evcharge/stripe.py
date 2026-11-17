"""Thin client for the Stripe REST calls we use.

Checkout with `payment_intent_data.capture_method=manual`: create a Checkout Session for
the hold -> the driver pays on Stripe's page -> the PaymentIntent waits in
`requires_capture` (the hold) -> capture the actual cost with `amount_to_capture`, which
releases the rest. Or cancel the PaymentIntent, if nothing was used.

The same shape as paypal.py, deliberately: plain httpx, no SDK, every POST carries an
Idempotency-Key derived from our session, so a retry after a timeout returns the
original result instead of doing it twice.

Amounts go to Stripe in the currency's minor unit (cents, pence).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx

from .config import Settings

API = "https://api.stripe.com"
# Stripe's own default tolerance for a webhook's timestamp.
SIGNATURE_TOLERANCE = 300


class StripeError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        self.code = err.get("code") or err.get("type")
        self.decline_code = err.get("decline_code")
        super().__init__(f"Stripe {status} {self.code or ''}".strip())


def minor_units(value: Decimal) -> int:
    return int((value * 100).quantize(Decimal("1"), ROUND_HALF_UP))


def from_minor(value: int | None) -> Decimal | None:
    return None if value is None else (Decimal(value) / 100).quantize(Decimal("0.01"))


def _flatten(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: {"a": {"b": 1}, "c": [x]} -> a[b]=1, c[0]=x."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out += _flatten(value, name)
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out += _flatten(item, f"{name}[{i}]")
                else:
                    out.append((f"{name}[{i}]", _scalar(item)))
        else:
            out.append((name, _scalar(value)))
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Stripe:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        headers = {"Stripe-Version": settings.stripe_api_version} if settings.stripe_api_version else {}
        self._http = httpx.Client(base_url=API, timeout=30, transport=transport, headers=headers)

    def _call(self, method: str, path: str, data: dict | None = None,
              idempotency_key: str | None = None, params: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.settings.stripe_secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        r = self._http.request(method, path, headers=headers,
                               data=dict(_flatten(data)) if data else None,
                               params=_flatten(params) if params else None)
        if r.status_code >= 400:
            raise StripeError(r.status_code, _body(r))
        return r.json() if r.content else {}

    def create_checkout(self, *, amount: Decimal, reference: str, description: str, product: str,
                        success_url: str, cancel_url: str, expires_at: int, idempotency_key: str) -> dict:
        """A Checkout Session that holds `amount` on the driver's card and captures nothing yet."""
        payment_intent: dict[str, Any] = {
            "capture_method": "manual",
            "description": description,
            "metadata": {"session_id": reference},
        }
        if self.settings.statement_name:
            # Appended to the account's statement descriptor prefix on the card statement.
            payment_intent["statement_descriptor_suffix"] = self.settings.statement_name
        return self._call("POST", "/v1/checkout/sessions", idempotency_key=idempotency_key, data={
            "mode": "payment",
            # "card" includes Apple Pay and Google Pay, and every card method supports a later capture.
            "payment_method_types": ["card"],
            "line_items": [{
                "quantity": 1,
                "price_data": {
                    "currency": self.settings.currency.lower(),
                    "unit_amount": minor_units(amount),
                    "product_data": {"name": product},
                },
            }],
            "payment_intent_data": payment_intent,
            "client_reference_id": reference,
            "metadata": {"session_id": reference},
            "success_url": success_url,
            "cancel_url": cancel_url,
            "expires_at": expires_at,
            "submit_type": "pay",
        })

    def retrieve_checkout(self, checkout_id: str) -> dict:
        """The Checkout Session, with its PaymentIntent as an object rather than an id."""
        return self._call("GET", f"/v1/checkout/sessions/{checkout_id}", params={"expand": ["payment_intent"]})

    def expire_checkout(self, checkout_id: str) -> dict:
        """Close a Checkout page nobody finished, so it can't be paid later."""
        return self._call("POST", f"/v1/checkout/sessions/{checkout_id}/expire")

    def capture(self, payment_intent_id: str, *, amount: Decimal, idempotency_key: str) -> dict:
        """Take `amount` of the hold; Stripe releases the rest. The charge's balance
        transaction is expanded so the fee Stripe kept can be recorded."""
        return self._call("POST", f"/v1/payment_intents/{payment_intent_id}/capture",
                          idempotency_key=idempotency_key, data={
                              "amount_to_capture": minor_units(amount),
                              "expand": ["latest_charge.balance_transaction"],
                          })

    def cancel(self, payment_intent_id: str, *, idempotency_key: str) -> dict:
        return self._call("POST", f"/v1/payment_intents/{payment_intent_id}/cancel",
                          idempotency_key=idempotency_key, data={"cancellation_reason": "abandoned"})


def verify_signature(payload: bytes, header: str, secret: str, *, tolerance: int = SIGNATURE_TOLERANCE,
                     now: float | None = None) -> bool:
    """Check a webhook's Stripe-Signature header: "t=<unix time>,v1=<hex HMAC-SHA256 of
    '<t>.<raw body>'>" (several v1 entries during a secret roll). Too old a timestamp
    fails too, so a captured request can't be replayed later."""
    if not (header and secret):
        return False
    timestamp, signatures = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    try:
        t = int(timestamp or "")
    except ValueError:
        return False
    if not signatures or abs((time.time() if now is None else now) - t) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in signatures)


def sign(payload: bytes, secret: str, t: int | None = None) -> str:
    """A Stripe-Signature header for `payload`: for tests and the simulator."""
    t = int(time.time()) if t is None else t
    sig = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:500]
