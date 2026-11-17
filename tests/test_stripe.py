"""Stripe as the payment provider: Checkout with capture_method=manual, partial capture,
cancel, webhooks, and the pay pages in euros."""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from evcharge.config import Settings, from_env
from evcharge.db import DB
from evcharge.payments import PaymentError, Payments
from evcharge.paypal import PayPal
from evcharge.stripe import Stripe, sign, verify_signature
from evcharge.tariff import Tariff
from evcharge.web import create_app
from tests.test_payments import StubCharger


class FakeStripe:
    """Enough of the Stripe API for the flows: Checkout Sessions, PaymentIntents with
    manual capture, and Idempotency-Key replay."""

    def __init__(self):
        self.checkouts = {}
        self.pis = {}
        self.calls = []
        self.seen = {}
        self.fail_next = {}          # path fragment -> (status, body)
        self.email = "driver@example.org"

    def pay(self, checkout_id: str, status: str = "requires_capture") -> str:
        """The driver completes Stripe's page: a PaymentIntent holding the amount."""
        c = self.checkouts[checkout_id]
        pid = f"pi_test_{len(self.pis) + 1}"
        self.pis[pid] = {"id": pid, "object": "payment_intent", "status": status, "amount": c["amount"],
                         "amount_capturable": c["amount"], "created": int(time.time()), "captured": None}
        c["status"], c["pi"] = "complete", pid
        return pid

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        assert req.headers["authorization"] == "Bearer sk_test_x"
        assert req.headers["stripe-version"] == "2024-06-20"
        for frag, (status, body) in list(self.fail_next.items()):
            if frag in path:
                del self.fail_next[frag]
                return httpx.Response(status, json=body)
        key = req.headers.get("idempotency-key")
        if key and key in self.seen:
            return self.seen[key]
        form = dict(parse_qsl(req.content.decode())) if req.content else {}
        resp = self._handle(req, path, form)
        if key:
            self.seen[key] = resp
        return resp

    def _handle(self, req, path, form):
        if req.method == "POST" and path == "/v1/checkout/sessions":
            cid = f"cs_test_{len(self.checkouts) + 1}"
            self.checkouts[cid] = {"form": form, "status": "open", "pi": None,
                                   "amount": int(form["line_items[0][price_data][unit_amount]"])}
            return httpx.Response(200, json={"id": cid, "object": "checkout.session", "status": "open",
                                             "url": f"https://checkout.stripe.com/c/pay/{cid}", "payment_intent": None})
        if m := re.fullmatch(r"/v1/checkout/sessions/(cs_test_\d+)", path):
            c = self.checkouts[m[1]]
            expanded = "payment_intent" in req.url.params.get_list("expand[0]")
            pi = self.pis.get(c["pi"]) if c["pi"] else None
            return httpx.Response(200, json={
                "id": m[1], "object": "checkout.session", "status": c["status"],
                "payment_intent": (pi if expanded else pi["id"]) if pi else None,
                "customer_details": {"email": self.email}})
        if m := re.fullmatch(r"/v1/checkout/sessions/(cs_test_\d+)/expire", path):
            c = self.checkouts[m[1]]
            if c["status"] != "open":
                return httpx.Response(400, json={"error": {"type": "invalid_request_error",
                                                           "message": "Only open sessions can be expired."}})
            c["status"] = "expired"
            return httpx.Response(200, json={"id": m[1], "status": "expired"})
        if m := re.fullmatch(r"/v1/payment_intents/(pi_test_\d+)/capture", path):
            pi = self.pis[m[1]]
            assert pi["status"] == "requires_capture", "capture on a PaymentIntent that holds nothing"
            amount = int(form["amount_to_capture"])
            assert 0 < amount <= pi["amount_capturable"]
            assert form["expand[0]"] == "latest_charge.balance_transaction"
            fee = int((Decimal(amount) * Decimal("0.015")).quantize(Decimal("1"), ROUND_HALF_UP)) + 25
            pi.update(status="succeeded", captured=amount, amount_capturable=0)
            return httpx.Response(200, json={**pi, "amount_received": amount, "latest_charge": {
                "id": f"ch_{m[1]}", "balance_transaction": {"id": f"txn_{m[1]}", "fee": fee}}})
        if m := re.fullmatch(r"/v1/payment_intents/(pi_test_\d+)/cancel", path):
            pi = self.pis[m[1]]
            if pi["status"] in ("succeeded", "canceled"):
                return httpx.Response(400, json={"error": {"code": "payment_intent_unexpected_state"}})
            pi["status"] = "canceled"
            return httpx.Response(200, json=pi)
        return httpx.Response(404, json={"error": {"type": "invalid_request_error"}})


STRIPE = dict(payment_provider="stripe", stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_test",
              currency="EUR", tariff=Tariff.flat(Decimal("0.40")), hold_options=(Decimal("50.00"),),
              ocpp_port=0, ocpp_allow_ws=frozenset({"CP1", "CP2"}), public_url="https://ev.example.org")


@pytest.fixture
def fake():
    return FakeStripe()


@pytest.fixture
def pay(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "s.db"), **STRIPE)
    return Payments(s, DB(s.db_path), PayPal(s), Stripe(s, transport=httpx.MockTransport(fake)))


def checkout_id(pay, sid):
    return pay.db.session(sid)["stripe_checkout_id"]


def authorised(pay, fake, connector="CP1"):
    sid, _ = pay.create_checkout(connector)
    cid = checkout_id(pay, sid)
    pid = fake.pay(cid)
    assert pay.confirm_checkout(cid)["status"] == "AUTHORISED"
    return sid, pid


def charging(pay, fake, connector="CP1"):
    sid, pid = authorised(pay, fake, connector)
    pay.mark_charging(sid)
    return sid, pid


# -- payments ---------------------------------------------------------------------

def test_checkout_holds_the_amount_without_capturing(pay, fake):
    sid, url = pay.create_checkout("CP1")
    cid = checkout_id(pay, sid)
    form = fake.checkouts[cid]["form"]
    assert url == f"https://checkout.stripe.com/c/pay/{cid}"
    assert form["mode"] == "payment" and form["payment_intent_data[capture_method]"] == "manual"
    assert form["line_items[0][price_data][unit_amount]"] == "5000"
    assert form["line_items[0][price_data][currency]"] == "eur"
    assert form["payment_method_types[0]"] == "card"
    assert form["metadata[session_id]"] == sid == form["client_reference_id"]
    assert form["payment_intent_data[metadata][session_id]"] == sid
    assert form["success_url"] == f"https://ev.example.org/pay/session/{sid}"
    assert form["cancel_url"] == "https://ev.example.org/pay/CP1"
    assert int(form["expires_at"]) >= int(time.time()) + 30 * 60      # Stripe's minimum
    assert form["payment_intent_data[description]"].startswith("Held up to €50.00")
    s = pay.db.session(sid)
    assert (s["status"], s["payment_method"], s["hold_amount"]) == ("CREATED", "stripe", "50.00")


def test_unknown_socket_and_hold_are_refused(pay):
    with pytest.raises(PaymentError) as e:
        pay.create_checkout("NOPE")
    assert e.value.code == 404
    with pytest.raises(PaymentError) as e:
        pay.create_checkout("CP1", Decimal("20"))
    assert e.value.code == 400


def test_confirm_only_once_paid_and_twice_is_harmless(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    cid = checkout_id(pay, sid)
    assert pay.confirm_checkout(cid)["status"] == "CREATED"           # still on Stripe's page
    assert not pay.connector_busy("CP1")
    pid = fake.pay(cid)
    s = pay.confirm_checkout(cid)
    assert (s["status"], s["stripe_payment_intent_id"]) == ("AUTHORISED", pid)
    assert s["authorization_expires"] and s["payer_email"] is None     # receipts off: no address kept
    assert pay.confirm_checkout(cid)["status"] == "AUTHORISED"
    assert pay.connector_busy("CP1")


def test_full_charge_captures_cost_and_records_fee(pay, fake):
    sid, pid = charging(pay, fake)
    s = pay.settle(sid, 12_500)                                         # 12.5 kWh at 40c
    assert (s["status"], s["amount_charged"], s["energy_wh"]) == ("CAPTURED", "5.00", 12_500)
    assert fake.pis[pid]["captured"] == 500
    assert s["paypal_fee"] == "0.33"                                    # 1.5 % + 25c, as Stripe reported it


def test_settle_twice_does_not_capture_twice(pay, fake):
    sid, _ = charging(pay, fake)
    pay.settle(sid, 1000)
    pay.settle(sid, 1000)
    assert sum(1 for m, p in fake.calls if p.endswith("/capture")) == 1


def test_cost_never_exceeds_hold(pay, fake):
    sid, pid = charging(pay, fake)
    assert pay.settle(sid, 200_000)["amount_charged"] == "50.00"
    assert fake.pis[pid]["captured"] == 5000


def test_no_energy_cancels_the_hold(pay, fake):
    sid, pid = charging(pay, fake)
    s = pay.settle(sid, 0)
    assert (s["status"], s["amount_charged"]) == ("VOIDED", "0.00")
    assert fake.pis[pid]["status"] == "canceled"


def test_capture_5xx_is_retryable_with_the_same_key(pay, fake):
    sid, pid = charging(pay, fake)
    fake.fail_next["/capture"] = (500, {"error": {"type": "api_error"}})
    with pytest.raises(PaymentError):
        pay.settle(sid, 2000)
    s = pay.db.session(sid)
    assert s["status"] == "CHARGING" and "retryable" in s["error"]
    assert pay.settle(sid, 2000)["status"] == "CAPTURED"
    assert fake.pis[pid]["captured"] == 80


def test_capture_4xx_is_final(pay, fake):
    sid, _ = charging(pay, fake)
    fake.fail_next["/capture"] = (400, {"error": {"code": "charge_expired_for_capture"}})
    with pytest.raises(PaymentError):
        pay.settle(sid, 2000)
    s = pay.db.session(sid)
    assert s["status"] == "FAILED" and "charge_expired_for_capture" in s["error"]


def test_second_driver_paying_for_a_taken_socket_is_released(pay, fake):
    first, _ = pay.create_checkout("CP1")
    second, _ = pay.create_checkout("CP1")                 # both on Stripe's page at once
    p1, p2 = fake.pay(checkout_id(pay, first)), fake.pay(checkout_id(pay, second))
    assert pay.confirm_checkout(checkout_id(pay, first))["status"] == "AUTHORISED"
    s = pay.confirm_checkout(checkout_id(pay, second))
    assert (s["status"], s["amount_charged"]) == ("VOIDED", "0.00")
    assert (fake.pis[p1]["status"], fake.pis[p2]["status"]) == ("requires_capture", "canceled")


def test_payment_not_held_is_refused_and_released(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    pid = fake.pay(checkout_id(pay, sid), status="requires_action")
    s = pay.confirm_checkout(checkout_id(pay, sid))
    assert s["status"] == "FAILED" and "requires_action" in s["error"]
    assert fake.pis[pid]["status"] == "canceled"


def test_expired_checkout_is_abandoned(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    fake.checkouts[checkout_id(pay, sid)]["status"] = "expired"
    assert pay.confirm_checkout(checkout_id(pay, sid))["status"] == "ABANDONED"


def test_cancelling_an_unpaid_checkout_closes_stripes_page(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    assert pay.cancel(sid, "driver went away")["status"] == "ABANDONED"
    assert fake.checkouts[checkout_id(pay, sid)]["status"] == "expired"


def test_unused_hold_is_released(pay, fake):
    sid, pid = authorised(pay, fake)
    pay.expire_unused(datetime.now(timezone.utc) + timedelta(minutes=20))
    assert pay.db.session(sid)["status"] == "VOIDED"
    assert fake.pis[pid]["status"] == "canceled"


def test_stripe_outage_while_confirming_is_not_final(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    fake.pay(checkout_id(pay, sid))
    fake.fail_next["/v1/checkout/sessions/"] = (503, {"error": {"type": "api_error"}})
    with pytest.raises(PaymentError) as e:
        pay.confirm_checkout(checkout_id(pay, sid))
    assert e.value.code == 502 and pay.db.session(sid)["status"] == "CREATED"
    assert pay.confirm_checkout(checkout_id(pay, sid))["status"] == "AUTHORISED"


# -- webhooks ---------------------------------------------------------------------

def event(etype, obj, eid=None):
    return {"id": eid or f"evt_{etype}_{obj.get('id')}", "type": etype, "data": {"object": obj}}


def test_webhook_completed_confirms_and_returns_the_session(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    cid = checkout_id(pay, sid)
    fake.pay(cid)
    note, s = pay.handle_stripe_webhook(event("checkout.session.completed", {"id": cid}))
    assert note == "checkout.session.completed" and s["status"] == "AUTHORISED"
    note, s = pay.handle_stripe_webhook(event("checkout.session.completed", {"id": cid}))  # redelivered
    assert s["status"] == "AUTHORISED"
    assert pay.handle_stripe_webhook(event("checkout.session.completed", {"id": "cs_other"}))[0] == "unmatched checkout"


def test_webhook_expired_and_cancelled(pay, fake):
    sid, _ = pay.create_checkout("CP1")
    pay.handle_stripe_webhook(event("checkout.session.expired", {"id": checkout_id(pay, sid)}))
    assert pay.db.session(sid)["status"] == "ABANDONED"
    held, pid = authorised(pay, fake, "CP2")
    ev = event("payment_intent.canceled", {"id": pid, "status": "canceled"})
    assert pay.handle_stripe_webhook(ev)[0] == "payment_intent.canceled"
    assert pay.db.session(held)["status"] == "VOIDED" and not pay.connector_busy("CP2")
    assert pay.handle_stripe_webhook(ev)[0] == "duplicate"
    assert pay.handle_stripe_webhook(event("customer.created", {"id": "cus_1"}))[0] == "ignored"


def test_signature():
    body = b'{"id": "evt_1"}'
    now = 1_800_000_000
    good = sign(body, "whsec_test", now)
    assert verify_signature(body, good, "whsec_test", now=now)
    assert not verify_signature(body, good, "whsec_other", now=now)
    assert not verify_signature(body + b" ", good, "whsec_test", now=now)
    assert not verify_signature(body, good, "whsec_test", now=now + 301)          # replayed later
    assert not verify_signature(body, "garbage", "whsec_test", now=now)
    assert not verify_signature(body, "", "whsec_test", now=now)
    rolled = f"{good},v1={'0' * 64}"                                               # during a secret roll
    assert verify_signature(body, rolled, "whsec_test", now=now)


# -- settings ---------------------------------------------------------------------

def test_settings_from_env():
    s = from_env({"PAYMENT_PROVIDER": "Stripe", "STRIPE_SECRET_KEY": "rk_live_x", "STRIPE_WEBHOOK_SECRET": "whsec_1",
                  "CURRENCY": "eur", "PRICE_PER_KWH": "0.40", "HOLD_OPTIONS": "50"})
    assert (s.payment_provider, s.currency, s.payments_configured, s.provider_name) == ("stripe", "EUR", True, "Stripe")
    assert s.stripe_api_version == "2024-06-20"
    assert not from_env({"PAYMENT_PROVIDER": "stripe"}).payments_configured
    for bad in ({"PAYMENT_PROVIDER": "sumup"}, {"STRIPE_SECRET_KEY": "pk_live_x"},
                {"STRIPE_WEBHOOK_SECRET": "abc"}, {"CURRENCY": "euro"}):
        with pytest.raises(ValueError):
            from_env(bad)


def test_money_formatting():
    eur, gbp, chf = Settings(currency="EUR"), Settings(), Settings(currency="CHF")
    assert (eur.money(Decimal("4.8")), eur.money(50, 0), eur.unit_price(Decimal("0.40"))) == ("€4.80", "€50", "€0.40")
    assert eur.unit_price(Decimal("0.255")) == "€0.255"
    assert (gbp.money("4.8"), gbp.unit_price(Decimal("0.45"))) == ("£4.80", "45p")
    assert chf.money(Decimal("1")) == "1.00 CHF"


# -- web --------------------------------------------------------------------------

class RecordingCharger(StubCharger):
    def __init__(self, answer="Accepted"):
        self.answer, self.started = answer, []

    async def remote_start(self, session, connector):
        self.started.append(session["id"])
        return self.answer


def plug(app, charger, socket="CP1", status="Preparing"):
    cp, conn = app.state.csms.settings.sockets[socket]
    app.state.csms.points[cp] = charger
    app.state.csms.status[(cp, conn)] = status


@pytest.fixture
def web(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "w.db"), **STRIPE)
    app = create_app(s, stripe=Stripe(s, transport=httpx.MockTransport(fake)))
    with TestClient(app) as c:
        charger = RecordingCharger()
        plug(app, charger)
        yield SimpleNamespace(client=c, app=app, charger=charger, db=app.state.payments.db)


def post_event(c, ev, secret="whsec_test"):
    body = json.dumps(ev).encode()
    return c.post("/webhooks/stripe", content=body,
                  headers={"Stripe-Signature": sign(body, secret), "Content-Type": "application/json"})


def test_pay_page_in_euros_without_paypal(web):
    page = web.client.get("/pay/CP1").text
    assert 'action="/pay/api/checkout"' in page and "Continue to payment" in page
    assert "€0.40 per kWh" in page and "€50" in page and "125 kWh" in page
    assert "paypal.com/sdk" not in page and "Your card holds an amount" in page
    index = web.client.get("/").text
    assert "You can hold €50." in index and "Apple Pay" in index


def test_web_flow_return_from_stripe_starts_the_charger(web, fake):
    r = web.client.post("/pay/api/checkout", data={"connector": "CP1", "hold": "50.00"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://checkout.stripe.com/c/pay/cs_test_")
    sid = web.db.recent_sessions(1)[0]["id"]
    assert "Waiting for Stripe approval" in web.client.get(f"/pay/session/{sid}").text
    fake.pay(web.db.session(sid)["stripe_checkout_id"])
    page = web.client.get(f"/pay/session/{sid}").text                  # back from Stripe, before any webhook
    assert "Starting the charger" in page and "€50.00" in page
    assert web.charger.started == [sid]
    web.client.get(f"/pay/session/{sid}")
    assert web.charger.started == [sid]                                  # started once only


def test_webhook_starts_the_charger(web, fake):
    web.client.post("/pay/api/checkout", data={"connector": "CP1"}, follow_redirects=False)
    s = web.db.recent_sessions(1)[0]
    fake.pay(s["stripe_checkout_id"])
    assert post_event(web.client, event("checkout.session.completed", {"id": s["stripe_checkout_id"]})).status_code == 200
    assert web.db.session(s["id"])["status"] == "AUTHORISED" and web.charger.started == [s["id"]]


def test_charger_refusing_the_start_releases_the_hold(web, fake):
    web.charger.answer = "Rejected"
    web.client.post("/pay/api/checkout", data={"connector": "CP1"}, follow_redirects=False)
    s = web.db.recent_sessions(1)[0]
    pid = fake.pay(s["stripe_checkout_id"])
    page = web.client.get(f"/pay/session/{s['id']}").text
    assert "did not start" in page and "You have not been charged" in page
    assert web.db.session(s["id"])["status"] == "VOIDED" and fake.pis[pid]["status"] == "canceled"


def test_webhook_signature_is_required(web):
    ev = event("checkout.session.completed", {"id": "cs_test_1"})
    assert post_event(web.client, ev, secret="whsec_wrong").status_code == 400
    assert web.client.post("/webhooks/stripe", json=ev).status_code == 400


def test_webhook_answers_503_while_stripe_is_down(web, fake):
    web.client.post("/pay/api/checkout", data={"connector": "CP1"}, follow_redirects=False)
    s = web.db.recent_sessions(1)[0]
    fake.pay(s["stripe_checkout_id"])
    fake.fail_next["/v1/checkout/sessions/"] = (502, {"error": {"type": "api_error"}})
    ev = event("checkout.session.completed", {"id": s["stripe_checkout_id"]})
    assert post_event(web.client, ev).status_code == 503                 # Stripe will redeliver
    assert post_event(web.client, ev).status_code == 200
    assert web.db.session(s["id"])["status"] == "AUTHORISED"


def test_checkout_needs_the_car_plugged_in(web, fake):
    plug(web.app, web.charger, status="Available")
    r = web.client.post("/pay/api/checkout", data={"connector": "CP1"}, follow_redirects=False)
    assert r.status_code == 200 and "plug your car in first" in r.text
    assert not fake.checkouts
    assert web.client.post("/pay/api/checkout", data={"connector": "NOPE"}).status_code == 404


def test_paypal_endpoints_are_off_with_stripe(web):
    assert web.client.post("/pay/api/orders", json={"connector": "CP1"}).status_code == 503


def test_webhook_not_configured(tmp_path, fake):
    s = Settings(db_path=str(tmp_path / "n.db"), **{**STRIPE, "stripe_webhook_secret": ""})
    with TestClient(create_app(s, stripe=Stripe(s, transport=httpx.MockTransport(fake)))) as c:
        assert c.post("/webhooks/stripe", json={}).status_code == 503
