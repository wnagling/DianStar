"""Trusted backend controls for future modes; never sourced from model arguments."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolPolicy:
    enabled: bool = True
    # Applies to actions with a trusted preparation/approval description.
    # Ordinary read grants and explicit revocations remain in Authorization.
    requires_approval: bool = True

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.requires_approval) is not bool:
            raise ValueError("Tool policy flags must be booleans")
