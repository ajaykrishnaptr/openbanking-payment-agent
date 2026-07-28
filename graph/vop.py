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
    reason: str | None = None   # only the semantic adapter fills this

    def as_dict(self) -> dict:
        payload = {
            "status": self.status,
            "matched_name": self.matched_name,
            "confidence": round(self.confidence, 3),
            "provider": self.provider,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


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


NAME_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_entity": {
            "type": "string",
            "enum": ["yes", "probably", "no"],
            "description": "Whether the two strings name the same legal entity.",
        },
        "confidence": {"type": "number", "description": "0 to 1."},
        "reason": {"type": "string", "description": "One short sentence, no more."},
    },
    "required": ["same_entity", "confidence", "reason"],
    "additionalProperties": False,
}

NAME_JUDGE_SYSTEM = """You compare a payee name against the name a bank holds for an \
account, for a payment about to be made in Europe.

Judge only whether the two strings denote the same legal entity. Treat these as the \
same entity: a legal form written differently or omitted (GmbH, Ltd, Limited, BV, SA), \
an ampersand against "and", initials against a full first name, a transliteration or a \
missing diacritic, a well-known abbreviation of the same registered name.

Treat these as different entities: a different company that merely shares a word, a \
person against a company, or a name that is close only by spelling accident.

You are not deciding whether the payment may proceed. You return a signal; the rules \
decide. Be conservative: when genuinely unsure, answer "probably" rather than "yes"."""


class SemanticVoP:
    """The stub's directory, with a model judging the near-misses.

    Structure matters here. The deterministic checks run first and settle the
    cases they can: an unknown account is uncheckable, an exact string match is
    a MATCH. The model is consulted only for the genuinely ambiguous middle,
    which is where a string ratio is weakest and language is the actual problem.

    If the model is unavailable, over budget, or fails, this falls back to
    StubVoP's ratio and the graph behaves exactly as it does today.
    """

    PROVIDER = "semantic-simulated"

    def __init__(self) -> None:
        self._stub = StubVoP()

    def verify(self, payee_name: str, account: dict) -> VoPResult:
        key = (account.get("sort_code"), account.get("account_number"))
        on_file = self._stub.DIRECTORY.get(key)

        if on_file is None:
            return VoPResult("MATCH_NOT_POSSIBLE", None, 0.0, self.PROVIDER)

        if payee_name.strip().lower() == on_file.lower():
            return VoPResult("MATCH", on_file, 1.0, self.PROVIDER)

        from . import llm

        verdict = llm.judge(
            system=NAME_JUDGE_SYSTEM,
            prompt=f"Payee name on the instruction: {payee_name}\nName the bank holds: {on_file}",
            schema=NAME_JUDGE_SCHEMA,
            max_tokens=1500,
        )

        if verdict is None:
            return self._stub.verify(payee_name, account)

        status = {"yes": "PARTIAL", "probably": "PARTIAL", "no": "NO_MATCH"}.get(
            verdict.get("same_entity"), "NO_MATCH"
        )
        confidence = float(verdict.get("confidence") or 0.0)
        result = VoPResult(status, on_file, confidence, self.PROVIDER)
        result.reason = verdict.get("reason")  # type: ignore[attr-defined]
        return result


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
    provider = os.getenv("VOP_PROVIDER", "stub")
    if provider == "truelayer":
        return TrueLayerVoP()
    if provider == "semantic":
        return SemanticVoP()
    return StubVoP()
