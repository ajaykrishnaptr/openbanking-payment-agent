"""Consent and risk rules. This is the part that is genuinely yours.

The rails are rented (TrueLayer), the payee check is simulated (see vop.py),
but what the agent is ALLOWED to do, and when it must stop and ask, lives
here in plain readable rules.
"""

from __future__ import annotations

from datetime import date, timedelta

# Stand-in for the consent table. In the real build this is Postgres, and the
# record binds to a real OAuth authorisation with a real expiry.
CONSENTS = {
    "user-001": {"consent_id": "cns_9f21", "scope": "payments", "expires": date.today() + timedelta(days=90)},
    "user-expired": {"consent_id": "cns_0001", "scope": "payments", "expires": date.today() - timedelta(days=1)},
}

# Payees this agent has paid before, and payees a human has flagged.
# Long-term memory in the spec; a dict until Postgres arrives.
KNOWN_PAYEES = {"Pinguin Pfannkuchen GmbH"}
FLAGGED_PAYEES = {"Waffelwerk Bremen GmbH"}

# Above this, a human decides regardless of how clean everything else looks.
AUTO_APPROVE_CEILING_MINOR = 100_000  # 1,000.00 in major units


def check_consent(user_id: str) -> dict:
    record = CONSENTS.get(user_id)
    if record is None:
        return {"status": "missing", "consent_id": None, "scope": None}
    if record["expires"] < date.today():
        return {"status": "expired", "consent_id": record["consent_id"], "scope": record["scope"]}
    return {"status": "valid", "consent_id": record["consent_id"], "scope": record["scope"]}


def assess_risk(payee_name: str, amount_minor: int, vop_status: str) -> list[str]:
    """Return the reasons a human should look at this. Empty list means the
    agent may proceed on its own."""
    flags = []

    # Each flag is written as a clause that can follow "because ...", so the
    # interface can put it in a sentence without reformatting it.
    if vop_status == "PARTIAL":
        flags.append("the payee name is a near match rather than an exact one")
    if amount_minor > AUTO_APPROVE_CEILING_MINOR:
        flags.append(f"the amount is above the auto-approve ceiling of {AUTO_APPROVE_CEILING_MINOR // 100:,}")
    if payee_name in FLAGGED_PAYEES:
        flags.append("this payee was flagged by a human before")
    if payee_name not in KNOWN_PAYEES and payee_name not in FLAGGED_PAYEES:
        flags.append("this is the first payment to this payee")

    return flags
