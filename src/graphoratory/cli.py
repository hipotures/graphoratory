from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from graphoratory.application import (
    WorkspaceStatus,
    create_line,
    create_workspace,
    generate_workspace_graphs,
    get_line_status,
    get_workspace_status,
    list_lines,
    list_workspaces,
    reindex_workspace,
)
from graphoratory.config import AppConfig, load_config
from graphoratory.errors import GraphoratoryError

app = typer.Typer(
    name="graphlab",
    help="Filesystem-first graph laboratory.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
workspace_app = typer.Typer(
    help="Create, inspect, list, and reindex workspaces.",
    no_args_is_help=True,
)
graph_app = typer.Typer(help="Generate the persistent graph corpus.", no_args_is_help=True)
line_app = typer.Typer(
    help="Create and inspect independent search lines.",
    no_args_is_help=True,
)
app.add_typer(workspace_app, name="workspace")
app.add_typer(graph_app, name="graph")
app.add_typer(line_app, name="line")

_CONSOLE = Console()
_ERROR_CONSOLE = Console(stderr=True)
_OVERRIDE_HELP = "Configuration overrides in key=value form."


@workspace_app.command("init")
def workspace_init(
    name: Annotated[str, typer.Argument(help="Human-readable workspace name.", metavar="NAME")],
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Create a workspace with a canonical typed ID."""

    def execute() -> None:
        config, operational = _command_config(overrides)
        _reject_operational(operational)
        typer.echo(create_workspace(config, name).display)

    _run(execute)


@workspace_app.command("list")
def workspace_list(
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """List workspaces from their authoritative manifests."""

    def execute() -> None:
        config, operational = _command_config(overrides)
        _reject_operational(operational)
        table = Table(box=None)
        table.add_column("NAME")
        table.add_column("ID")
        table.add_column("CREATED")
        table.add_column("ACTIVE")
        for workspace in list_workspaces(config):
            table.add_row(
                workspace.name or "—",
                workspace.identifier.display,
                workspace.created_at,
                "*" if workspace.active else "",
            )
        _CONSOLE.print(table)

    _run(execute)


@workspace_app.command("status")
def workspace_status(
    workspace: Annotated[
        str | None,
        typer.Argument(
            help="Workspace name or lowercase typed workspace ID.",
            metavar="WORKSPACE",
        ),
    ] = None,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Show read-only workspace status."""

    def execute() -> None:
        target, command_overrides = _positional_or_assignment(workspace, overrides)
        config, operational = _command_config(command_overrides, {"workspace"})
        target = _explicit_target(target, operational.pop("workspace", None), "workspace")
        _reject_operational(operational)
        status = get_workspace_status(config, target)
        _status_table(_workspace_status_rows(status))

    _run(execute)


@workspace_app.command("reindex")
def workspace_reindex(
    workspace: Annotated[
        str | None,
        typer.Argument(
            help="Workspace name or lowercase typed workspace ID.",
            metavar="WORKSPACE",
        ),
    ] = None,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Rebuild SQLite from authoritative workspace artifacts."""

    def execute() -> None:
        target, command_overrides = _positional_or_assignment(workspace, overrides)
        config, operational = _command_config(command_overrides, {"workspace"})
        target = _explicit_target(target, operational.pop("workspace", None), "workspace")
        _reject_operational(operational)
        identifier = reindex_workspace(config, target)
        status = get_workspace_status(config, identifier.display)
        _status_table(
            _workspace_status_rows(status),
            title="[green]Reindex complete[/green]",
        )

    _run(execute)


@graph_app.command("generate")
def graph_generate(
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Generate and persist one workspace graph corpus."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        result = generate_workspace_graphs(config, workspace)
        typer.echo(f"generated {result.graph_count} graphs")
        if result.rejected or result.duplicates:
            _ERROR_CONSOLE.print(
                f"candidate attempts: {result.attempts}; "
                f"invalid candidates: {result.rejected}; "
                f"duplicate candidates: {result.duplicates}"
            )

    _run(execute)


@line_app.command("create")
def line_create(
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Create a line in the selected workspace."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        typer.echo(create_line(config, workspace).display)

    _run(execute)


@line_app.command("list")
def line_list(
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """List lines in the selected workspace."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        result = list_lines(config, workspace)
        workspace_label = result.workspace_name or result.workspace.display
        _CONSOLE.print(
            f"[bold]Workspace:[/bold] {workspace_label} ({result.workspace.display})"
        )
        if not result.lines:
            _CONSOLE.print(f"No lines in workspace {workspace_label}.")
            return
        table = Table(box=None)
        table.add_column("ID")
        table.add_column("CREATED")
        table.add_column("GRAPHS", justify="right")
        table.add_column("LATEST")
        for line in result.lines:
            table.add_row(
                line.identifier.display,
                line.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
                str(line.graph_count),
                "*" if line.latest else "",
            )
        _CONSOLE.print(table)

    _run(execute)


@line_app.command("status")
def line_status(
    line: Annotated[
        str | None,
        typer.Argument(help="Lowercase typed line ID.", metavar="LINE"),
    ] = None,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Show status for a line, defaulting to the latest in the selected workspace."""

    def execute() -> None:
        target, command_overrides = _positional_or_assignment(line, overrides)
        config, operational = _command_config(command_overrides, {"line", "workspace"})
        target = _explicit_target(target, operational.pop("line", None), "line")
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        status = get_line_status(config, target, workspace)
        line_label = status.identifier.display
        if status.selected_latest:
            workspace_label = status.workspace_name or status.workspace.display
            line_label = f"{line_label} (latest in workspace {workspace_label})"
        _status_table(
            [
                ("Line", line_label),
                ("Workspace", status.workspace.display),
                ("Graphs", str(status.graph_count)),
                ("Created", status.created_at),
                ("Phase", status.phase),
                ("Database", status.database_state),
            ]
        )

    _run(execute)


def main() -> None:
    app(prog_name="graphlab")


def _run(action: Callable[[], None]) -> None:
    try:
        action()
    except (GraphoratoryError, OSError, ValueError) as exc:
        _ERROR_CONSOLE.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(2) from exc


def _command_config(
    arguments: list[str] | None,
    operational_keys: set[str] | None = None,
) -> tuple[AppConfig, dict[str, str]]:
    assignments = _parse_assignments(arguments or [])
    config_path = Path(assignments.pop("config", "experiment.toml"))
    keys = operational_keys or set()
    operational = {
        key: assignments.pop(key) for key in tuple(assignments) if key in keys
    }
    config = load_config(config_path, [f"{key}={value}" for key, value in assignments.items()])
    return config, operational


def _parse_assignments(arguments: list[str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for argument in arguments:
        if "=" not in argument:
            raise ValueError(f"configuration overrides must use key=value syntax: {argument!r}")
        key, value = argument.split("=", 1)
        if not key or not value:
            raise ValueError(f"invalid key=value override: {argument!r}")
        if key in assignments:
            raise ValueError(f"override was provided more than once: {key}")
        assignments[key] = value
    return assignments


def _positional_or_assignment(
    target: str | None,
    overrides: list[str] | None,
) -> tuple[str | None, list[str]]:
    values = list(overrides or [])
    if target is not None and "=" in target:
        values.insert(0, target)
        return None, values
    return target, values


def _explicit_target(
    positional: str | None,
    assignment: str | None,
    name: str,
) -> str | None:
    if positional is not None and assignment is not None:
        raise ValueError(f"{name} was selected more than once")
    return positional if positional is not None else assignment


def _reject_operational(values: dict[str, str]) -> None:
    if values:
        raise ValueError(f"unexpected command parameter: {sorted(values)[0]}")


def _workspace_status_rows(status: WorkspaceStatus) -> list[tuple[str, str]]:
    return [
        ("Name", status.name or "—"),
        ("Workspace", status.identifier.display),
        ("Created", status.created_at),
        ("Config", status.config_source),
        ("Generator", status.generator or "—"),
        ("Graphs", str(status.graph_count)),
        (
            "Order range",
            f"{status.min_order}..{status.max_order}"
            if status.min_order is not None
            else "—",
        ),
        ("Lines", str(status.line_count)),
        ("Database", status.database_state),
        ("Disk usage", _format_bytes(status.disk_bytes)),
    ]


def _status_table(
    rows: list[tuple[str, str]],
    *,
    title: str | None = None,
) -> None:
    table = Table(title=title, show_header=False, box=None, padding=(0, 2))
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
