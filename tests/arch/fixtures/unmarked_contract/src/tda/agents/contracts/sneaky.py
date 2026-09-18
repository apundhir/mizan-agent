"""VIOLATION FIXTURE - the dodge.

Declaring an output contract as a bare BaseModel rather than an AgentOutput would escape the
numeric rule entirely while sitting in the contracts package looking exactly like a contract.
That is the shortcut somebody takes after the lint blocks them once.
"""

from pydantic import BaseModel


class ResolutionResult(BaseModel):
    iso2: str
    confidence: float  # would be a VIOLATION if the class were marked
