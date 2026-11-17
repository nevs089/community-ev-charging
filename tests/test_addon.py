"""The Home Assistant add-on: its default options must give the app valid settings,
and its start script must map options to the environment without leaking secrets."""
import importlib.util
import json
from pathlib import Path

import yaml

from evcharge.config import from_env

ADDON = Path(__file__).resolve().parent.parent / "evcharge_addon"
spec = importlib.util.spec_from_file_location("addon_start", ADDON / "start.py")
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


def config():
    return yaml.safe_load((ADDON / "config.yaml").read_text())


def test_every_option_has_a_schema_entry():
    c = config()
    assert set(c["options"]) == set(c["schema"])
    assert c["ports"] == {"9000/tcp": 9000, "8080/tcp": 8081}


def test_defaults_give_valid_settings():
    s = from_env(start.environment(config()["options"]))
    assert (s.payment_provider, s.currency, s.connectors) == ("stripe", "EUR", ("CP1",))
    assert s.sockets == {"CP1": ("CP1", 1)} and s.ocpp_allow_ws == frozenset({"CP1"})
    assert s.trusted_nets == ("100.64.0.0/10",) and not s.payments_configured


def test_options_for_a_real_charger():
    opts = {**config()["options"], "charge_point_id": "SN123", "stripe_secret_key": "rk_test_abc",
            "stripe_webhook_secret": "whsec_abc", "public_url": "https://ev.example.org",
            "ocpp_password": "pw-secret", "admin_password": "admin-secret", "socket_kw": 7}
    env = start.environment(opts)
    assert env["SOCKETS"] == "CP1=SN123:1" and env["OCPP_ALLOW_WS_SN123"] == "true"
    assert env["OCPP_PASSWORD_SN123"] == "pw-secret" and env["SOCKET_KW_CP1"] == "7"
    s = from_env(env)
    assert s.payments_configured and s.public_url == "https://ev.example.org"
    shown = start.describe(opts, env)
    for secret in ("rk_test_abc", "whsec_abc", "pw-secret", "admin-secret"):
        assert secret not in shown
    assert "(set)" in shown


def test_dockerfile_pins_a_commit():
    text = (ADDON / "Dockerfile").read_text()
    ref = [l for l in text.splitlines() if l.startswith("ARG EVCHARGE_REF=")][0].split("=", 1)[1]
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
    json.dumps(config())        # plain data, nothing yaml-specific
