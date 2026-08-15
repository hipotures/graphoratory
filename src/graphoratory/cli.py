from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.table import Table

from graphoratory.application import (
    create_line,
    create_workspace,
    generate_graph_corpus,
    get_line_status,
    get_workspace_status,
    reindex_workspace,
)
from graphoratory.config import AppConfig, load_config
from graphoratory.errors import GraphoratoryError

Command = Callable[[AppConfig, dict[str, str]], None]
_CONSOLE = Console()
_ERROR_CONSOLE = Console(stderr=True)


def main() -> None:
    try:
        command, assignments = _parse_command(sys.argv[1:])
        config_path = Path(assignments.pop("config", "experiment.toml"))
        operational_keys = _operational_keys(command)
        operational = {
            key: assignments.pop(key) for key in tuple(assignments) if key in operational_keys
        }
        config = load_config(config_path, [f"{key}={value}" for key, value in assignments.items()])
        command(config, operational)
    except (GraphoratoryError, OSError, ValueError) as exc:
        _ERROR_CONSOLE.print(f"[red]error:[/red] {exc}")
        raise SystemExit(2) from exc


def _parse_command(arguments: list[str]) -> tuple[Command, dict[str, str]]:
    if not arguments or arguments[0] in {"-h", "--help", "help"}:
        _print_help()
        raise SystemExit(0)
    if len(arguments) < 2:
        raise ValueError("expected a command group and action; use graphlab --help")
    command = _COMMANDS.get((arguments[0], arguments[1]))
    if command is None:
        raise ValueError(f"unknown command: {' '.join(arguments[:2])}")
    assignments: dict[str, str] = {}
    for argument in arguments[2:]:
        if "=" not in argument:
            raise ValueError(f"parameters must use key=value syntax: {argument!r}")
        key, value = argument.split("=", 1)
        if not key or not value:
            raise ValueError(f"invalid key=value parameter: {argument!r}")
        if key in assignments:
            raise ValueError(f"parameter was provided more than once: {key}")
        assignments[key] = value
    return command, assignments


def _operational_keys(command: Command) -> set[str]:
    if command is _workspace_init:
        return set()
    if command in {_workspace_status, _workspace_reindex, _graph_generate}:
        return {"workspace"}
    if command is _line_create:
        return {"workspace", "corpus"}
    if command is _line_status:
        return {"line"}
    return set()


def _workspace_init(config: AppConfig, values: dict[str, str]) -> None:
    _reject_operational(values)
    print(create_workspace(config).display)


def _workspace_status(config: AppConfig, values: dict[str, str]) -> None:
    workspace = _required(values, "workspace")
    _reject_operational(values)
    status = get_workspace_status(config, workspace)
    rows = [
        ("Workspace", status.identifier.display),
        ("Created", status.created_at),
        ("Config", status.config_source),
        ("Corpora", str(status.corpus_count)),
        ("Graphs", str(status.graph_count)),
        (
            "Order range",
            f"{status.min_order}..{status.max_order}" if status.min_order is not None else "—",
        ),
        ("Lines", str(status.line_count)),
        ("Database", status.database_state),
        ("Disk usage", _format_bytes(status.disk_bytes)),
    ]
    _status_table(rows)


def _workspace_reindex(config: AppConfig, values: dict[str, str]) -> None:
    workspace = _required(values, "workspace")
    _reject_operational(values)
    identifier = reindex_workspace(config, workspace)
    print(identifier.display)


def _graph_generate(config: AppConfig, values: dict[str, str]) -> None:
    workspace = _required(values, "workspace")
    _reject_operational(values)
    result = generate_graph_corpus(config, workspace)
    print(result.identifier.display)
    if result.duplicates:
        _ERROR_CONSOLE.print(
            f"generation attempts: {result.attempts}; duplicate attempts: {result.duplicates}"
        )


def _line_create(config: AppConfig, values: dict[str, str]) -> None:
    workspace = _required(values, "workspace")
    corpus = values.pop("corpus", None)
    _reject_operational(values)
    identifier = create_line(
        config,
        workspace,
        corpus,
    )
    print(identifier.display)


def _line_status(config: AppConfig, values: dict[str, str]) -> None:
    line = _required(values, "line")
    _reject_operational(values)
    status = get_line_status(config, line)
    _status_table(
        [
            ("Line", status.identifier.display),
            ("Workspace", status.workspace.display),
            ("Corpus", status.corpus.display),
            ("Graphs", str(status.graph_count)),
            ("Created", status.created_at),
            ("Phase", status.phase),
            ("Database", status.database_state),
        ]
    )


def _required(values: dict[str, str], key: str) -> str:
    value = values.pop(key, None)
    if value is None:
        raise ValueError(f"missing required parameter: {key}=...")
    return value


def _reject_operational(values: dict[str, str]) -> None:
    if values:
        raise ValueError(f"unexpected command parameter: {sorted(values)[0]}")


def _status_table(rows: list[tuple[str, str]]) -> None:
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for name, value in rows:
        table.add_row(name, value)
    _CONSOLE.print(table)


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _print_help() -> None:
    _CONSOLE.print(
        """[bold]graphlab[/bold] COMMAND [key=value ...]

Commands:
  workspace init
  workspace status workspace=ws-xxxxxxxx
  workspace reindex workspace=ws-xxxxxxxx
  graph generate workspace=ws-xxxxxxxx
  line create workspace=ws-xxxxxxxx [corpus=cp-xxxxxxxx]
  line status line=ln-xxxxxxxx

All commands load experiment.toml by default. Use config=PATH for another file."""
    )


_COMMANDS: dict[tuple[str, str], Command] = {
    ("workspace", "init"): _workspace_init,
    ("workspace", "status"): _workspace_status,
    ("workspace", "reindex"): _workspace_reindex,
    ("graph", "generate"): _graph_generate,
    ("line", "create"): _line_create,
    ("line", "status"): _line_status,
}
