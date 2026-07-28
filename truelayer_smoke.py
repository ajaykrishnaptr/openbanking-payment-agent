"""
Smoke test for the real TrueLayer PIS connector.

Proves the pipeline incrementally:
  1. OAuth client-credentials token        (needs client_id/secret)        -> always
  2. Request signing + LOCAL verify         (needs the private key)         -> always
  3. Live POST /v3/payments + HPP url        (needs TRUELAYER_KID registered) -> if kid set

Run:  direnv exec . python3 truelayer_smoke.py
"""
import json
import os
import uuid

from truelayer_signing import HttpMethod
from connectors import get_connector

# Whatever you registered in the TrueLayer console. Set RETURN_URI to override.
RETURN_URI = os.environ.get("RETURN_URI", "https://openbanking-payment-agent.vercel.app/callback")

c = get_connector("truelayer")

print("1) OAuth token …")
tok = c.access_token()
print(f"   OK · access_token <{len(tok)} chars> · scope=payments\n")

print("2) request signing + local verify …")
idem = str(uuid.uuid4())
body = json.dumps({"amount_in_minor": 4250, "currency": "GBP", "demo": True})
sig = c.sign(HttpMethod.POST, "/v3/payments", idem, body)
c.verify_local(HttpMethod.POST, "/v3/payments", idem, body, sig)   # raises if bad
print(f"   OK · Tl-Signature <{len(sig)} chars> · self-verified against public key\n")

if not c.kid:
    print("3) live payment — SKIPPED (TRUELAYER_KID not set).")
    print("   Upload secrets/ec_public_key.pem in the TrueLayer console, paste the")
    print("   'kid' into .envrc, run `direnv allow`, and re-run this script.")
    raise SystemExit(0)

print("3) live POST /v3/payments (sandbox Mock Bank) …")
status, payload, sent = c.create_payment(
    amount_minor=4250, currency="GBP",
    beneficiary={
        "type": "external_account",
        "account_holder_name": "Nike Store EU",
        "account_identifier": {"type": "sort_code_account_number",
                               "sort_code": "040668", "account_number": "00000871"},
        "reference": "agent-order-001",
    },
    user={"id": str(uuid.uuid4()), "name": "Ajay Krishna",
          "email": "psu@example.com"},
    return_uri=RETURN_URI,
)
print(f"   HTTP {status}")
print("   " + json.dumps(payload, indent=2)[:900])
if status in (200, 201):
    pid, rt = payload["id"], payload["resource_token"]
    print(f"\n   payment id: {pid}\n   status: {payload.get('status')}")
    print("   HPP (PSU completes SCA here):")
    print("   " + c.hpp_url(pid, rt, RETURN_URI))
