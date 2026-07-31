"""
RENTED connectivity layer — a real PSD2 / Open Banking PIS provider (TrueLayer sandbox).

This is the "rent connectivity, own authority" architecture: the bank-facing plumbing
(licence, eIDAS-equivalent request signing, ASPSP fan-out) is rented from a TPP. The
agent-authority layer (scoped, revocable, key-bound mandate) sits ABOVE this and is
provider-agnostic — see authority/.

Real here: OAuth client-credentials token, TrueLayer request signing (Tl-Signature,
ES512/P-521), the live /v3/payments create call, and payment-status polling against the
sandbox. Synthetic: the funds (sandbox test money) and the Mock Bank PSU.
"""
from __future__ import annotations

import json
import os
import uuid

import requests
from truelayer_signing import HttpMethod, sign_with_pem, verify_with_pem

AUTH_BASE = "https://auth.truelayer-sandbox.com"
API_BASE = "https://api.truelayer-sandbox.com"
HPP_BASE = "https://payment.truelayer-sandbox.com"
PAYMENTS_PATH = "/v3/payments"
MANDATES_PATH = "/v3/mandates"
# sweeping (me-to-me VRP) needs this scope on the token; the client must be enabled for it
SWEEPING_SCOPE = "payments recurring_payments:sweeping"


class TrueLayerConnector:
    name = "truelayer-sandbox"

    def __init__(self) -> None:
        self.client_id = os.environ["TRUELAYER_CLIENT_ID"]
        self.client_secret = os.environ["TRUELAYER_CLIENT_SECRET"]
        self.kid = os.environ.get("TRUELAYER_KID", "")
        # TRUELAYER_PRIVATE_KEY is either the PEM contents (env var on a host) or a file
        # path (local). TRUELAYER_PRIVATE_KEY_PEM is an explicit PEM-contents override.
        pem = os.environ.get("TRUELAYER_PRIVATE_KEY_PEM")
        key_ref = os.environ.get("TRUELAYER_PRIVATE_KEY", "")
        if pem:
            self.private_key = pem
        elif "BEGIN" in key_ref:
            self.private_key = key_ref
        elif key_ref:
            with open(key_ref, encoding="utf-8") as fh:
                self.private_key = fh.read()
        else:
            raise RuntimeError("set TRUELAYER_PRIVATE_KEY_PEM (PEM contents) "
                               "or TRUELAYER_PRIVATE_KEY (file path)")

    # --- OAuth: client-credentials access token (scope=payments by default) ---
    def access_token(self, scope: str = "payments") -> str:
        r = requests.post(
            f"{AUTH_BASE}/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": scope,
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    # --- TrueLayer request signing (Tl-Signature) over method+path+headers+body ---
    def sign(self, method: HttpMethod, path: str, idempotency_key: str, body_str: str) -> str:
        return (
            sign_with_pem(self.kid, self.private_key)
            .set_method(method)
            .set_path(path)
            .add_header("Idempotency-Key", idempotency_key)
            .set_body(body_str)
            .sign()
        )

    def verify_local(self, method: HttpMethod, path: str, idempotency_key: str,
                     body_str: str, signature: str) -> None:
        """Self-check: verify our own signature with the public key (no network)."""
        with open(os.environ["TRUELAYER_PRIVATE_KEY"].replace("private", "public"),
                  encoding="utf-8") as fh:
            pub = fh.read()
        (verify_with_pem(pub)
         .set_method(method)
         .set_path(path)
         .add_header("Idempotency-Key", idempotency_key)
         .set_body(body_str)
         .verify(signature))

    # --- create a single immediate payment to an external account (PIS) ---
    def create_payment(self, *, amount_minor: int, currency: str, beneficiary: dict,
                        user: dict, return_uri: str,
                        idempotency_key: str | None = None) -> tuple[int, dict, dict]:
        token = self.access_token()
        # A caller that can be re-run has to supply its own key: a fresh one per
        # attempt is a fresh payment as far as the provider is concerned. Only
        # callers that genuinely run once should let this default.
        idem = idempotency_key or str(uuid.uuid4())
        body = {
            "amount_in_minor": amount_minor,
            "currency": currency,
            "payment_method": {
                "type": "bank_transfer",
                "provider_selection": {
                    "type": "preselected",
                    "provider_id": "mock-payments-gb-redirect",
                    "scheme_selection": {"type": "instant_preferred"},
                },
                "beneficiary": beneficiary,
            },
            "user": user,
            "hosted_page": {
                "return_uri": return_uri,
                "country_code": "GB",
                "language_code": "en",
            },
        }
        body_str = json.dumps(body)
        sig = self.sign(HttpMethod.POST, PAYMENTS_PATH, idem, body_str)
        r = requests.post(
            f"{API_BASE}{PAYMENTS_PATH}",
            data=body_str,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": idem,
                "Tl-Signature": sig,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text}
        return r.status_code, payload, body

    def hpp_url(self, payment_id: str, resource_token: str, return_uri: str) -> str:
        return (f"{HPP_BASE}/payments#payment_id={payment_id}"
                f"&resource_token={resource_token}&return_uri={return_uri}")

    def get_payment(self, payment_id: str) -> tuple[int, dict]:
        token = self.access_token()
        r = requests.get(
            f"{API_BASE}{PAYMENTS_PATH}/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        return r.status_code, (r.json() if r.text else {})

    # =======================================================================
    # VRP (sweeping) — authorize ONCE, then pay many times with NO re-SCA.
    # This is the real-rail primitive behind the multi-use mandate demo.
    # =======================================================================
    def create_sweeping_mandate(self, *, currency: str, merchant: str, user: dict,
                                max_individual_minor: int = 50000,
                                monthly_cap_minor: int = 150000,
                                valid_from: str, valid_to: str) -> tuple[int, dict, dict]:
        """Create a sweeping VRP mandate. The user authorizes it ONCE (one SCA)."""
        token = self.access_token(SWEEPING_SCOPE)
        idem = str(uuid.uuid4())
        body = {
            "mandate": {
                "type": "sweeping",
                "provider_selection": {"type": "preselected",
                                       "provider_id": "mock-payments-gb-redirect"},
                "beneficiary": {
                    "type": "external_account",
                    "account_holder_name": merchant,
                    "account_identifier": {"type": "sort_code_account_number",
                                           "sort_code": "040668",
                                           "account_number": "00000871"},
                },
            },
            "currency": currency,
            "user": user,
            "constraints": {
                "valid_from": valid_from,
                "valid_to": valid_to,
                "maximum_individual_amount": max_individual_minor,
                "periodic_limits": {"month": {"maximum_amount": monthly_cap_minor,
                                              "period_alignment": "calendar"}},
            },
        }
        body_str = json.dumps(body)
        idem_k = idem
        sig = self.sign(HttpMethod.POST, MANDATES_PATH, idem_k, body_str)
        r = requests.post(
            f"{API_BASE}{MANDATES_PATH}",
            data=body_str,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_k,
                     "Tl-Signature": sig, "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            payload = r.json()
        except ValueError:
            payload = {"raw": r.text}
        return r.status_code, payload, body

    def mandate_hpp_url(self, mandate_id: str, resource_token: str, return_uri: str) -> str:
        return (f"{HPP_BASE}/mandates#mandate_id={mandate_id}"
                f"&resource_token={resource_token}&return_uri={return_uri}")

    def get_mandate(self, mandate_id: str) -> tuple[int, dict]:
        token = self.access_token(SWEEPING_SCOPE)
        r = requests.get(
            f"{API_BASE}{MANDATES_PATH}/{mandate_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        return r.status_code, (r.json() if r.text else {})

    def pay_on_mandate(self, *, amount_minor: int, currency: str, mandate_id: str,
                       reference: str) -> tuple[int, dict]:
        """Create a payment AGAINST an authorized mandate — no user redirect / no SCA."""
        token = self.access_token(SWEEPING_SCOPE)
        idem = str(uuid.uuid4())
        body = {
            "amount_in_minor": amount_minor,
            "currency": currency,
            "payment_method": {"type": "mandate", "mandate_id": mandate_id},
            "reference": reference[:18],
        }
        body_str = json.dumps(body)
        idem_k = idem
        sig = self.sign(HttpMethod.POST, PAYMENTS_PATH, idem_k, body_str)
        r = requests.post(
            f"{API_BASE}{PAYMENTS_PATH}",
            data=body_str,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": idem_k,
                     "Tl-Signature": sig, "Content-Type": "application/json"},
            timeout=30,
        )
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text}

    # --- normalized provider interface (shared with other connectors) ---
    SIGNED = "ES512 Tl-Signature"

    def initiate(self, *, amount_minor: int, currency: str, merchant: str,
                 reference: str, user: dict, return_uri: str,
                 idempotency_key: str | None = None) -> dict:
        status, payload, _ = self.create_payment(
            amount_minor=amount_minor, currency=currency,
            beneficiary={"type": "external_account", "account_holder_name": merchant,
                         "account_identifier": {"type": "sort_code_account_number",
                                                "sort_code": "040668", "account_number": "00000871"},
                         "reference": reference},
            user=user, return_uri=return_uri, idempotency_key=idempotency_key)
        if status not in (200, 201):
            return {"ok": False, "provider": self.name, "http": status, "error": payload}
        return {"ok": True, "provider": self.name, "id": payload["id"],
                "auth_url": self.hpp_url(payload["id"], payload["resource_token"], return_uri),
                "status": "pending", "raw_status": payload.get("status"), "signed": self.SIGNED}

    def status_of(self, payment_id: str) -> dict:
        _, data = self.get_payment(payment_id)
        raw = data.get("status")
        norm = "executed" if raw in ("executed", "settled") else (
            "failed" if raw == "failed" else "pending")
        return {"status": norm, "raw_status": raw}
