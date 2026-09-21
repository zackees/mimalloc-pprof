#!/usr/bin/env python3
"""Keep scaling rendering behind its validated, typed dataclass boundary."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path

TYPED_FUNCTIONS = frozenset(
    {
        "scaling_svg",
        "distribution_global_domain",
        "distribution_stack_svg",
        "render_scaling_html",
    }
)


@dataclass(frozen=True, slots=True)
class Violation:
    line: int
    message: str


class TypedRendererVisitor(ast.NodeVisitor):
    def __init__(self, checked_functions: set[str]) -> None:
        self.checked_functions = checked_functions
        self.current_function: str | None = None
        self.violations: list[Violation] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        previous = self.current_function
        self.current_function = node.name if node.name in self.checked_functions else None
        if self.current_function is not None:
            if node.name in TYPED_FUNCTIONS:
                first = node.args.args[0] if node.args.args else None
                annotation = ast.unparse(first.annotation) if first and first.annotation else ""
                if annotation != "ScalingView":
                    self.violations.append(
                        Violation(node.lineno, f"{node.name} must accept ScalingView")
                    )
            self.generic_visit(node)
        self.current_function = previous

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name in self.checked_functions:
            self.violations.append(
                Violation(node.lineno, f"{node.name} must be a synchronous typed renderer")
            )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if (
            self.current_function is not None
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
            and not (isinstance(node.value, ast.Name) and node.value.id.isupper())
        ):
            self.violations.append(
                Violation(
                    node.lineno,
                    f"{self.current_function} uses dynamic string-key dictionary access; use a typed field",
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if (
            self.current_function is not None
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and not (isinstance(node.func.value, ast.Name) and node.func.value.id.isupper())
        ):
            self.violations.append(
                Violation(
                    node.lineno,
                    f"{self.current_function} uses dynamic .get(); use a typed field",
                )
            )
        self.generic_visit(node)


def check_source(source: str) -> list[Violation]:
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    checked_functions = set(TYPED_FUNCTIONS)
    pending = list(TYPED_FUNCTIONS)
    while pending:
        name = pending.pop()
        function = functions.get(name)
        if function is None:
            continue
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in functions
                and call.func.id not in checked_functions
            ):
                checked_functions.add(call.func.id)
                pending.append(call.func.id)
    visitor = TypedRendererVisitor(checked_functions)
    visitor.visit(tree)
    found = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in TYPED_FUNCTIONS
    }
    for missing in sorted(TYPED_FUNCTIONS - found):
        visitor.violations.append(Violation(1, f"missing typed renderer function {missing}"))
    return visitor.violations


def selftest() -> None:
    good = (
        "def scaling_svg(scaling: ScalingView, pattern: str):\n    return scaling.thread_points\n"
    )
    bad = "def scaling_svg(scaling: Mapping[str, object], pattern: str):\n    return scaling['thread_points']\n"
    # Supply the other required functions so the fixture isolates the intended checks.
    support = """
def distribution_global_domain(scaling: ScalingView, metric: str): pass
def distribution_stack_svg(scaling: ScalingView, pattern: str, metric: str): pass
def render_scaling_html(scaling: ScalingView): pass
"""
    assert not check_source(good + support)
    messages = [violation.message for violation in check_source(bad + support)]
    assert any("must accept ScalingView" in message for message in messages)
    assert any("dynamic string-key" in message for message in messages)
    helper_bad = good + support + "\ndef helper(value):\n    return value['field']\n"
    helper_bad = helper_bad.replace("return scaling.thread_points", "return helper(scaling)")
    assert any(
        "helper uses dynamic string-key" in violation.message
        for violation in check_source(helper_bad)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("path", nargs="?", default="ci/benchmark_report.py")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        print("PASS scaling typed-renderer checker selftest")
        return 0
    path = Path(args.path)
    violations = check_source(path.read_text(encoding="utf-8"))
    for violation in violations:
        print(f"{path}:{violation.line}: STR001 {violation.message}")
    if violations:
        return 1
    print("PASS scaling renderers use typed fields")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
