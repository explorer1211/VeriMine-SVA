"""Diagnostics shared by all pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, order=True)
class Issue:
    code: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        loc = f"{self.where}: " if self.where else ""
        return f"[{self.code}] {loc}{self.message}"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "where": self.where}


class StageError(Exception):
    """Raised when a stage cannot continue. Carries every error found, not just the first."""

    def __init__(self, issues: list[Issue], exit_code: int = 1):
        self.issues = issues
        self.exit_code = exit_code
        super().__init__("\n".join(str(i) for i in issues))


@dataclass
class IssueLog:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def error(self, code: str, message: str, where: str = "") -> None:
        self.errors.append(Issue(code, message, where))

    def warn(self, code: str, message: str, where: str = "") -> None:
        self.warnings.append(Issue(code, message, where))

    def raise_if_errors(self) -> None:
        if self.errors:
            raise StageError(self.errors)
