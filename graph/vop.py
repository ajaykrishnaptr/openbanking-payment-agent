"""Verification of Payee, behind an adapter.

Why an adapter instead of a direct call: TrueLayer's VoP service is not
available on this client. The `verification` scope resolves to the Data API
(audience `data_api`, account-holder verification via AIS consent), every
payee-verification path on the Payments API returns 404, and TrueLayer's
public position as of July 2026 is that the full VoP service arrives in H2
2026 with specifications still unpublished.

So the decision layer is built against an interface, and the only
implementation today is a simulation. When the provider ships, add
TrueLayerVoP.verify and change which adapter get_vop_adapter returns.

Statuses follow the SEPA VoP vocabulary:
  MATCH               name matches the account holder
  PARTIAL             close but not exact (initials, legal form, typo)
  NO_MATCH            different party
  MATCH_NOT_POSSIBLE  the account cannot be checked at all
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol


@dataclass
class VoPResult:
    status: str
    matched_name: str | None
    confidence: float
    provider: str

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "matched_name": self.matched_name,
            "confidence": round(self.confidence, 3),
            "provider": self.provider,
        }


class VoPAdapter(Protocol):
    def verify(self, payee_name: str, account: dict) -> VoPResult: ...


def _normalise(name: str) -> str:
    """Strip the noise that should not decide a payment: case, punctuation,
    and legal form. 'Pinguin Pfannkuchen GmbH' and 'pinguin pfannkuchen ltd'
    differ only in legal form, which is a PARTIAL, not a NO_MATCH."""
    cleaned = name.lower().replace(".", " ").replace(",", " ")
    legal_forms = {"gmbh", "ltd", "limited", "bv", "nv", "sa", "srl", "ag", "plc", "ug", "kg", "inc"}
    words = [w for w in cleaned.split() if w not in legal_forms]
    return " ".join(words)


class StubVoP:
    """Deterministic simulation. No network call, no provider.

    The directory is what a real VoP responder would hold: the name the bank
    has on file for an account. Keys are the account identifiers the
    TrueLayer sandbox mock bank uses.
    """

    PROVIDER = "stub-simulated"

    DIRECTORY = {
        ("040668", "00000871"): "Pinguin Pfannkuchen GmbH",
        ("040668", "00000872"): "Waffelwerk Bremen GmbH",
    }

    def verify(self, payee_name: str, account: dict) -> VoPResult:
        key = (account.get("sort_code"), account.get("account_number"))
        on_file = self.DIRECTORY.get(key)

        # The bank has no record of this account, so no check is possible.
        # This is a distinct answer from "the name is wrong" and the graph
        # must treat it differently.
        if on_file is None:
            return VoPResult("MATCH_NOT_POSSIBLE", None, 0.0, self.PROVIDER)

        if payee_name.strip().lower() == on_file.lower():
            return VoPResult("MATCH", on_file, 1.0, self.PROVIDER)

        ratio = SequenceMatcher(None, _normalise(payee_name), _normalise(on_file)).ratio()
        if ratio >= 0.85:
            return VoPResult("PARTIAL", on_file, ratio, self.PROVIDER)
        return VoPResult("NO_MATCH", on_file, ratio, self.PROVIDER)


class TrueLayerVoP:
    """Placeholder for the real service. Deliberately raises rather than
    quietly falling back, so nobody ships a demo believing this ran."""

    PROVIDER = "truelayer"

    def verify(self, payee_name: str, account: dict) -> VoPResult:
        raise NotImplementedError(
            "TrueLayer VoP is not available on this client (checked 2026-07-28). "
            "Provider timeline: H2 2026, specifications unpublished."
        )


def get_vop_adapter() -> VoPAdapter:
    """One line to change when the provider ships."""
    if os.getenv("VOP_PROVIDER", "stub") == "truelayer":
        return TrueLayerVoP()
    return StubVoP()
