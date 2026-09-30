"""校验结果。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Issue:
    path: str  # JSON 路径，如 $.episodes[0].scenes[1].shots[2]
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class Report:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def ok(self, strict: bool = False) -> bool:
        return not self.errors and not (strict and self.warnings)

    def to_dict(self) -> dict:
        return {
            "errors": [{"path": i.path, "message": i.message} for i in self.errors],
            "warnings": [{"path": i.path, "message": i.message} for i in self.warnings],
        }
