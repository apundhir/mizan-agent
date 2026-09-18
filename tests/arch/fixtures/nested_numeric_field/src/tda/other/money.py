"""A perfectly ordinary domain model, in a package the lint's contracts rule does not cover.

Nothing here is wrong. It carries numbers because it is about numbers, and it derives from
`BaseModel` because it is not an agent's answer. The violation is that a contract points at it.
"""

from pydantic import BaseModel


class Money(BaseModel):
    amount: float
    minor_units: int
