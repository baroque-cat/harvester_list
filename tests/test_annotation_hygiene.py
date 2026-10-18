"""Annotation hygiene: every name used in a type annotation must be bound.

Regression pin for the production incident of 2026-10-18. ``manager/pipeline.py``
used ``Dict[str, Any]`` in the return annotation of ``get_refine_metrics``
(added in ``e6d81ea`` / R1, 2026-09-22) while its typing import only ever
listed ``Dict, List, Optional``. The development and gate interpreter is
Python 3.14, where PEP 649 makes annotations LAZY: the missing import was
structurally invisible — 727 tests and every live gate passed for a month.
The production host runs pyenv 3.11.15, where annotations are evaluated
EAGERLY at class-body execution, so ``main.py`` died with
``NameError: name 'Any' is not defined`` before executing a single line.

Why this pin is AST-based and not import-based: importing the module under
3.14 cannot fail on an unbound annotation name (lazy evaluation), so only a
static check protects the class regardless of which interpreter runs the
suite. Same doctrine as ``_RENDERED_METRIC_SURFACES`` (ROI-S4): make the
defect impossible, not merely unlikely.

Approximation (documented, deliberate): a name counts as BOUND if it is
bound at module level (imports, assignments, defs, classes, with/for
targets, except-handler names) OR appears anywhere in the same file as a
parameter name or assignment target (nested-function annotations legally
evaluate against enclosing function locals) OR is a builtin. String
annotations (forward references) are data, not expressions, and are skipped.
The over-approximation trades a little strictness for zero false positives;
the incident name is still caught because ``Any`` was neither imported nor
assigned anywhere in pipeline.py. The second pin re-checks the incident file
STRICTLY at module level.
"""

import ast
import builtins
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "tests",        # dev-only; run under the dev interpreter by definition
    "openspec",
    "examples",
    "docs",
    "data",
    "logs",
}
BUILTIN_NAMES = frozenset(dir(builtins))


def _product_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _collect_bound(tree: ast.Module) -> set:
    """Module-level bindings plus every parameter/assignment target in the file."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            args = node.args
            for arg in (
                list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                + ([args.vararg] if args.vararg else [])
                + ([args.kwarg] if args.kwarg else [])
            ):
                bound.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def _collect_module_level_bound(tree: ast.Module) -> set:
    """Strict variant: only bindings executed at module top level."""
    bound = set()
    for node in tree.body:  # module body only, no descent
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


def _annotation_expressions(tree: ast.Module):
    """Yield every annotation expression node (args, returns, AnnAssign)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            holders = (
                list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                + ([args.vararg] if args.vararg else [])
                + ([args.kwarg] if args.kwarg else [])
            )
            for arg in holders:
                if arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            yield node.annotation


def _referenced_names(annotation: ast.expr):
    """Names loaded inside an annotation; string forward-refs contribute none."""
    if isinstance(annotation, ast.Constant):  # "ForwardRef" string annotation
        return
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            yield node.id, getattr(node, "lineno", 0)


def _violations(path: str, strict_module_level: bool = False):
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    bound = (
        _collect_module_level_bound(tree) if strict_module_level else _collect_bound(tree)
    )
    allowed = bound | BUILTIN_NAMES
    found = []
    for annotation in _annotation_expressions(tree):
        for name, lineno in _referenced_names(annotation):
            if name not in allowed:
                rel = os.path.relpath(path, REPO_ROOT)
                found.append(f"{rel}:{lineno}: annotation name '{name}' is never bound")
    return found


def test_every_product_annotation_name_is_bound():
    """No product module may annotate with a name it never binds.

    Under eager-annotation interpreters (<= 3.13, i.e. the production host's
    3.11.15) an unbound annotation name is an import-time NameError; under
    3.14 it is silently lazy. The static check holds on both.
    """
    violations = []
    scanned = 0
    for path in _product_files():
        scanned += 1
        violations.extend(_violations(path))
    assert scanned > 50, f"scan covered only {scanned} files — walk is broken"
    assert violations == [], "unbound annotation names:\n" + "\n".join(violations)


def test_regression_pipeline_binds_any_at_module_level():
    """Incident triple-lock: manager/pipeline.py must bind every annotation
    name at MODULE level (strict), so ``Dict[str, Any]`` at :294 can never
    again depend on an interpreter that defers evaluation."""
    path = os.path.join(REPO_ROOT, "manager", "pipeline.py")
    violations = _violations(path, strict_module_level=True)
    assert violations == [], "manager/pipeline.py:\n" + "\n".join(violations)


def test_product_sources_parse_under_python_311_syntax():
    """The production interpreter is pyenv 3.11.15 while dev/gates run 3.14.

    Best-effort static guard against syntax drift: every product source must
    parse with ``feature_version=(3, 11)``. Runtime stdlib differences are
    NOT covered here — the pre-deploy 3.11 import sweep (AGENTS.md §5) is the
    empirical layer for those.
    """
    broken = []
    scanned = 0
    for path in _product_files():
        scanned += 1
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        try:
            ast.parse(source, filename=path, feature_version=(3, 11))
        except SyntaxError as exc:
            rel = os.path.relpath(path, REPO_ROOT)
            broken.append(f"{rel}:{exc.lineno}: {exc.msg}")
    assert scanned > 50, f"scan covered only {scanned} files — walk is broken"
    assert broken == [], "sources not parseable as Python 3.11:\n" + "\n".join(broken)
