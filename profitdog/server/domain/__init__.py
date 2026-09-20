"""Derived conclusions: lives, kits, ledgers, metrics, correlations.

Nothing in here is stored. Everything is computed from facts on read, under a
ruleset chosen per match, which is what lets a rule change disagree with a
conclusion reached last month without touching a single recorded fact.
"""

from . import rulesets

__all__ = ["rulesets"]
