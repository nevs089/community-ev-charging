"""Add-on entry point: turn the add-on options (/data/options.json) into the
environment the app reads (env.example), then run it. Secrets are never logged."""
import json
import os
import sys

OPTIONS = os.environ.get("OPTIONS_PATH", "/data/options.json")

# Option -> environment variable, where the value passes through unchanged.
DIRECT = {
    "stripe_secret_key": "STRIPE_SECRET_KEY",
    "stripe_webhook_secret": "STRIPE_WEBHOOK_SECRET",
    "currency": "CURRENCY",
    "price_per_kwh": "PRICE_PER_KWH",
    "session_fee": "SESSION_FEE",
    "hold_options": "HOLD_OPTIONS",
    "public_url": "PUBLIC_URL",
    "admin_password": "ADMIN_PASSWORD",
    "api_token": "API_TOKEN",
    "timezone": "SITE_TIMEZONE",
    "trusted_networks": "TRUSTED_NETS",
    "brand_name": "BRAND_NAME",
    "brand_color": "BRAND_COLOR",
    "brand_logo": "BRAND_LOGO",
    "footer_text": "FOOTER_TEXT",
    "page_note": "PAGE_NOTE",
    "support_contact": "SUPPORT_CONTACT",
    "statement_name": "STATEMENT_NAME",
    "receipts_smtp_user": "SMTP_USER",
    "receipts_smtp_password": "SMTP_PASSWORD",
    "receipts_mail_from": "MAIL_FROM",
}
SECRET = {"stripe_secret_key", "stripe_webhook_secret", "admin_password", "api_token",
          "ocpp_password", "receipts_smtp_password"}


def environment(options: dict) -> dict[str, str]:
    env = {"PAYMENT_PROVIDER": "stripe"}
    for key, name in DIRECT.items():
        value = options.get(key)
        if value not in (None, ""):
            env[name] = str(value).strip()
    socket = options.get("socket_name") or "CP1"
    cp = (options.get("charge_point_id") or "").strip() or socket
    env["CONNECTORS"] = socket
    env["SOCKETS"] = f"{socket}={cp}:1"
    if options.get("socket_kw"):
        env[f"SOCKET_KW_{socket}"] = str(int(options["socket_kw"]))
    if options.get("ocpp_allow_ws"):
        env[f"OCPP_ALLOW_WS_{cp}"] = "true"
    if options.get("ocpp_password"):
        env[f"OCPP_PASSWORD_{cp}"] = options["ocpp_password"]
    return env


def describe(options: dict, env: dict[str, str]) -> str:
    shown = {k: ("(set)" if k in SECRET and v else v) for k, v in options.items()}
    return f"evcharge add-on: socket {env['SOCKETS']}, options {json.dumps(shown, ensure_ascii=False)}"


def main() -> None:
    try:
        with open(OPTIONS, encoding="utf-8") as f:
            options = json.load(f)
    except FileNotFoundError:
        options = {}
    env = environment(options)
    print(describe(options, env), flush=True)
    if not env.get("STRIPE_SECRET_KEY"):
        print("evcharge add-on: no stripe_secret_key yet - the pay pages say 'not set up yet'", flush=True)
    os.environ.update(env)
    os.chdir(os.environ.get("APP_DIR", "/srv"))
    # No --proxy-headers: the admin guard relies on the real source address (and on
    # Cf-Connecting-Ip being present for anything that came through the tunnel).
    args = ["uvicorn", "--factory", "evcharge.web:create_app", "--host", "0.0.0.0", "--port", "8080",
            "--no-server-header"]
    os.execvp(args[0], args)


if __name__ == "__main__":
    sys.exit(main())
