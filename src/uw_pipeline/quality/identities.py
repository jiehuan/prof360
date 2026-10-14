"""Relations that must hold between metrics of the same period (decisions D-042).

Configured in config/dq_rules.yaml (`metric_identities`). The textmetrics stage drops
text-extracted values that break one (a wrong number is worse than a missing one); the dq
stage reports any stored values that break one, XBRL values included.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping

from pydantic import BaseModel, Field, model_validator

# llm_task_stat task under which textmetrics records identity checks (generated = periods
# checked, kept = periods that held).
IDENTITY_TASK = "identity_check"

_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}


class Identity(BaseModel):
    """`left` ≈ `right` within `tolerance` (relative), or `ascending` metrics in order."""

    name: str
    left: str | None = None
    right: str | None = None  # an expression of metric codes: + - * / and parentheses
    tolerance: float = 0.05
    ascending: list[str] = Field(default_factory=list)
    annualize: list[str] = Field(default_factory=list)  # flows x4 in a quarter (NIM, D-044)

    @model_validator(mode="after")
    def _shape(self) -> Identity:
        if bool(self.left and self.right) == bool(self.ascending):
            raise ValueError(f"identity {self.name}: give left/right or ascending")
        if self.right:
            ast.parse(self.right, mode="eval")
        return self

    def metrics(self) -> set[str]:
        if self.ascending:
            return set(self.ascending)
        assert self.left and self.right
        names = {n.id for n in ast.walk(ast.parse(self.right, mode="eval"))
                 if isinstance(n, ast.Name)}  # fmt: skip
        return {self.left} | names

    def check(self, values: Mapping[str, float], period_type: str = "FY") -> str | None:
        """None when it holds or a value is missing; otherwise what is wrong."""
        if not self.metrics() <= values.keys():
            return None
        if period_type == "Q" and self.annualize:
            values = {k: v * 4 if k in self.annualize else v for k, v in values.items()}
        if self.ascending:
            pairs = zip(self.ascending, self.ascending[1:], strict=False)
            for a, b in pairs:
                if values[a] > values[b] + 1e-9:
                    return f"{a} {values[a]:.4g} > {b} {values[b]:.4g}"
            return None
        assert self.left and self.right
        expected = _eval(ast.parse(self.right, mode="eval").body, values)
        actual = values[self.left]
        if expected == 0:
            return None if actual == 0 else f"{self.left} {actual:.4g} vs {self.right} = 0"
        error = abs(actual - expected) / abs(expected)
        if error <= self.tolerance:
            return None
        return (f"{self.left} {actual:.4g} vs {self.right} = {expected:.4g} "
                f"({error:.1%} apart, allowed {self.tolerance:.0%})")  # fmt: skip


class LoanBookCheck(BaseModel):
    """The loan book by class may not add up to more than total loans (double counting)."""

    total: str = "total_loans"
    max_over: float = 0.01


class Identities(BaseModel):
    severity: str = "Error"
    periods: int = 8  # latest periods checked by the dq stage
    checks: list[Identity] = Field(default_factory=list)
    loan_book: LoanBookCheck = Field(default_factory=LoanBookCheck)


def _eval(node: ast.AST, values: Mapping[str, float]) -> float:
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left, values), _eval(node.right, values))
    if isinstance(node, ast.Name):
        return float(values[node.id])
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return float(node.value)
    raise ValueError(f"unsupported expression: {ast.dump(node)}")
