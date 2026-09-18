"""VIOLATION FIXTURE - asking an agent to classify.

Plausible-looking and fatal: it makes the hotel-error-versus-policy-disagreement decision
non-reproducible, which is the one decision the whole report rests on.
"""

from tda.agents.mapping import MappingAgent


def classify(variance: object) -> str:
    return MappingAgent().classify(variance)
