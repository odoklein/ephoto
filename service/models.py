"""Shared result types for the processing modules and the dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field

PASS, FAIL, UNKNOWN = "pass", "fail", "unknown"


@dataclass
class Check:
    """One conformity criterion.

    `unknown` exists on purpose and is never folded into `pass`: when a detector is
    unavailable (no landmark model, no face found) the criterion is undecided, and the
    human reviewer must be shown that rather than a green dot the code cannot justify.
    """

    key: str
    label: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PASS


def score(checks: list[Check]) -> int:
    """Percentage of the criteria that could be measured and passed.

    Undecided criteria are excluded from the denominator so that a missing detector
    lowers confidence in the report, not the score of a photograph that may be fine.
    """
    measured = [check for check in checks if check.status != UNKNOWN]
    if not measured:
        return 0
    return round(100 * sum(check.ok for check in measured) / len(measured))


@dataclass
class ProcessedImage:
    """A cleaned export plus everything the dashboard and the webhook need."""

    data: bytes
    media_type: str
    extension: str
    width: int
    height: int
    checks: list[Check] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    error: str = ""

    @property
    def score(self) -> int:
        return score(self.checks)

    @property
    def compliant(self) -> bool:
        return not self.error and all(check.status != FAIL for check in self.checks)

    def as_report(self) -> dict:
        return {
            "score": self.score,
            "compliant": self.compliant,
            "error": self.error,
            "width": self.width,
            "height": self.height,
            "bytes": len(self.data),
            "media_type": self.media_type,
            "checks": [
                {"key": c.key, "label": c.label, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
            "metadata": self.metadata,
        }
