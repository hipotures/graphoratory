# ruff: noqa: I001

import ast
from pathlib import Path


SRC = Path(__file__).parents[1] / "src" / "graphoratory"

# Filesystem enumeration is intentionally restricted. Shallow workspace discovery is the
# project-level exception; deep line enumeration is reindex-only; rglob is a disk-usage metric.
_ALLOWED_ENUMERATION = {
    ("application.py", "_directory_size", "rglob"),
    ("artifacts.py", "scan_evaluation_artifacts", "iterdir"),
    ("artifacts.py", "scan_workspace_directories", "iterdir"),
    ("artifacts.py", "scan_line_artifacts", "iterdir"),
}


class _EnumerationVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.functions: list[str] = []
        self.found: list[tuple[str, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"iterdir", "glob", "rglob"}:
            function = self.functions[-1] if self.functions else "<module>"
            self.found.append((self.relative_path, function, node.func.attr))
        self.generic_visit(node)


def test_filesystem_enumeration_is_confined_to_explicit_boundaries() -> None:
    found: set[tuple[str, str, str]] = set()
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        visitor = _EnumerationVisitor(relative)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        found.update(visitor.found)

    assert found == _ALLOWED_ENUMERATION


def test_application_never_uses_project_root_as_database_scope() -> None:
    application = (SRC / "application.py").read_text(encoding="utf-8")
    core = (SRC / "database" / "core.py").read_text(encoding="utf-8")

    assert "database_path(config.project_root)" not in application
    assert "project_root / DATABASE_NAME" not in core
    assert "def database_path(workspace_path: Path)" in core
    assert "return workspace_path / DATABASE_NAME" in core


def test_line_artifact_scan_is_reindex_only() -> None:
    application = (SRC / "application.py").read_text(encoding="utf-8")
    core = (SRC / "database" / "core.py").read_text(encoding="utf-8")

    assert "scan_line_artifacts" not in application
    assert "for line in scan_line_artifacts(workspace):" in core


def test_evaluation_artifact_scan_is_reindex_only() -> None:
    application = (SRC / "application.py").read_text(encoding="utf-8")
    core = (SRC / "database" / "core.py").read_text(encoding="utf-8")

    assert "scan_evaluation_artifacts" not in application
    assert "for evaluation in scan_evaluation_artifacts(workspace):" in core
