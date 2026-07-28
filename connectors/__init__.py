"""Connectivity layer (RENTED). The graph and its decisions (OWNED) live above
this, so the provider can be swapped without touching the agent logic.

Trimmed from the agent-payment-authority version: TrueLayer only. Add another
connector here if a second provider is ever needed.
"""
from .truelayer import TrueLayerConnector


def get_connector(name: str = "truelayer"):
    if name in ("truelayer", "truelayer-sandbox"):
        return TrueLayerConnector()
    raise ValueError(f"unknown connector: {name}")
