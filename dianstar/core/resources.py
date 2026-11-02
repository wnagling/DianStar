"""Trusted hierarchical resource claims, independent of concrete tools."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceClaim:
    domain: str
    key: tuple[str, ...] = ()
    write: bool = False

    def __post_init__(self):
        if not self.domain or not isinstance(self.key, tuple) or any(not isinstance(part, str) for part in self.key):
            raise ValueError("Invalid resource claim")

    def conflicts(self, other):
        length = min(len(self.key), len(other.key))
        return (self.domain == other.domain and (self.write or other.write)
                and self.key[:length] == other.key[:length])


def conflicts(left, right):
    return any(a.conflicts(b) for a in left for b in right)
