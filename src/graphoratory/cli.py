from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from typer._click.exceptions import ClickException
from typer.core import TyperGroup

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
from graphoratory.identifiers import Identifier


class JsonErrorGroup(TyperGroup):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        raw_args = list(args) if args is not None else sys.argv[1:]
        if "--json" not in raw_args:
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        try:
            result = super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except ClickException as exc:
            if not standalone_mode:
                raise
            _echo_json_error(exc.format_message(), type(exc).__name__)
            raise SystemExit(exc.exit_code) from exc
        if standalone_mode and isinstance(result, int) and result != 0:
            raise SystemExit(result)
        return result


app = typer.Typer(
    name="graphlab",
    help="Filesystem-first graph laboratory.",
    cls=JsonErrorGroup,
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
_JSON_HELP = "Emit machine-readable JSON instead of Rich output."


@workspace_app.command("init")
def workspace_init(
    name: Annotated[str, typer.Argument(help="Human-readable workspace name.", metavar="NAME")],
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Create a workspace with a canonical typed ID."""

    def execute() -> None:
        config, operational = _command_config(overrides)
        _reject_operational(operational)
        identifier = create_workspace(config, name)
        payload = {"workspace": _identifier_payload(identifier)}
        _emit(payload, json_output, lambda data: typer.echo(data["workspace"]["id"]))

    _run(execute, json_output)


@workspace_app.command("list")
def workspace_list(
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """List workspaces from the project index."""

    def execute() -> None:
        config, operational = _command_config(overrides)
        _reject_operational(operational)
        payload = {
            "workspaces": [
                {
                    **_identifier_payload(workspace.identifier),
                    "name": workspace.name,
                    "created_at": workspace.created_at,
                    "active": workspace.active,
                }
                for workspace in list_workspaces(config)
            ]
        }
        _emit(payload, json_output, _render_workspace_list)

    _run(execute, json_output)


def _render_workspace_list(payload: dict[str, Any]) -> None:
    table = Table(box=None)
    table.add_column("NAME")
    table.add_column("ID")
    table.add_column("CREATED")
    table.add_column("ACTIVE")
    for workspace in payload["workspaces"]:
        table.add_row(
            workspace["name"] or "—",
            workspace["id"],
            workspace["created_at"],
            "*" if workspace["active"] else "",
        )
    _CONSOLE.print(table)


@workspace_app.command("status")
def workspace_status(
    workspace: Annotated[
        str | None,
        typer.Argument(
            help="Workspace name or lowercase typed workspace ID.",
            metavar="WORKSPACE",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Show read-only workspace status."""

    def execute() -> None:
        target, command_overrides = _positional_or_assignment(workspace, overrides)
        config, operational = _command_config(command_overrides, {"workspace"})
        target = _explicit_target(target, operational.pop("workspace", None), "workspace")
        _reject_operational(operational)
        status = get_workspace_status(config, target)
        payload = _workspace_status_payload(status)
        _emit(payload, json_output, _render_workspace_status)

    _run(execute, json_output)


@workspace_app.command("reindex")
def workspace_reindex(
    workspace: Annotated[
        str | None,
        typer.Argument(
            help="Workspace name or lowercase typed workspace ID.",
            metavar="WORKSPACE",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Rebuild the project index from all workspace artifacts."""

    def execute() -> None:
        target, command_overrides = _positional_or_assignment(workspace, overrides)
        config, operational = _command_config(command_overrides, {"workspace"})
        target = _explicit_target(target, operational.pop("workspace", None), "workspace")
        _reject_operational(operational)
        identifier = reindex_workspace(config, target)
        status = get_workspace_status(config, identifier.display)
        payload = {
            "reindexed": True,
            **_workspace_status_payload(status),
        }
        _emit(
            payload,
            json_output,
            lambda data: _render_workspace_status(
                data,
                title="[green]Reindex complete[/green]",
            ),
        )

    _run(execute, json_output)


@graph_app.command("generate")
def graph_generate(
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Generate and persist one workspace graph corpus."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        result = generate_workspace_graphs(config, workspace)
        payload = {
            "workspace": _identifier_payload(result.workspace),
            "graph_count": result.graph_count,
            "attempted_candidates": result.attempts,
            "rejected_invalid_candidates": result.rejected,
            "duplicate_candidates": result.duplicates,
            "accepted_by_generator": dict(result.accepted_by_generator),
        }
        _emit(payload, json_output, _render_graph_result)

    _run(execute, json_output)


@line_app.command("create")
def line_create(
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """Create a line in the selected workspace."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        identifier = create_line(config, workspace)
        payload = {"line": _identifier_payload(identifier)}
        _emit(payload, json_output, lambda data: typer.echo(data["line"]["id"]))

    _run(execute, json_output)


@line_app.command("list")
def line_list(
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
    overrides: Annotated[list[str] | None, typer.Argument(help=_OVERRIDE_HELP)] = None,
) -> None:
    """List lines in the selected workspace."""

    def execute() -> None:
        config, operational = _command_config(overrides, {"workspace"})
        workspace = operational.pop("workspace", None)
        _reject_operational(operational)
        result = list_lines(config, workspace)
        payload = {
            "workspace": {
                **_identifier_payload(result.workspace),
                "name": result.workspace_name,
            },
            "lines": [
                {
                    **_identifier_payload(line.identifier),
                    "created_at": line.created_at.isoformat().replace("+00:00", "Z"),
                    "graphs": line.graph_count,
                    "latest": line.latest,
                }
                for line in result.lines
            ],
        }
        _emit(payload, json_output, _render_line_list)

    _run(execute, json_output)


@line_app.command("status")
def line_status(
    line: Annotated[
        str | None,
        typer.Argument(help="Lowercase typed line ID.", metavar="LINE"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help=_JSON_HELP)] = False,
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
        payload = {
            "line": _identifier_payload(status.identifier),
            "workspace": {
                **_identifier_payload(status.workspace),
                "name": status.workspace_name,
            },
            "graphs": status.graph_count,
            "created_at": status.created_at,
            "phase": status.phase,
            "database": status.database_state,
            "selected_latest": status.selected_latest,
        }
        _emit(payload, json_output, _render_line_status)

    _run(execute, json_output)


def main() -> None:
    app(prog_name="graphlab")


def _echo_json_error(message: str, error_type: str) -> None:
    typer.echo(
        json.dumps(
            {
                "error": {
                    "message": message,
                    "type": error_type,
                }
            },
            sort_keys=True,
        ),
        err=True,
    )


def _run(action: Callable[[], None], json_output: bool) -> None:
    try:
        action()
    except (GraphoratoryError, OSError, ValueError) as exc:
        if json_output:
            _echo_json_error(str(exc), type(exc).__name__)
        else:
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


def _identifier_payload(identifier: Identifier) -> dict[str, str]:
    return {
        "id": identifier.display,
        "hash": identifier.digest,
    }


def _workspace_status_payload(status: WorkspaceStatus) -> dict[str, Any]:
    return {
        "workspace": {
            **_identifier_payload(status.identifier),
            "name": status.name,
        },
        "created_at": status.created_at,
        "config": status.config_source,
        "generator": status.generator,
        "graphs": status.graph_count,
        "order_range": {
            "min": status.min_order,
            "max": status.max_order,
        },
        "lines": status.line_count,
        "database": status.database_state,
        "disk_bytes": status.disk_bytes,
    }


def _emit(
    payload: dict[str, Any],
    json_output: bool,
    rich_renderer: Callable[[dict[str, Any]], None],
) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        rich_renderer(payload)


def _render_graph_result(payload: dict[str, Any]) -> None:
    typer.echo(f"generated {payload['graph_count']} graphs")
    if payload["rejected_invalid_candidates"] or payload["duplicate_candidates"]:
        _ERROR_CONSOLE.print(
            f"candidate attempts: {payload['attempted_candidates']}; "
            f"invalid candidates: {payload['rejected_invalid_candidates']}; "
            f"duplicate candidates: {payload['duplicate_candidates']}"
        )


def _render_workspace_status(
    payload: dict[str, Any],
    *,
    title: str | None = None,
) -> None:
    _status_table(_workspace_status_rows(payload), title=title)


def _workspace_status_rows(payload: dict[str, Any]) -> list[tuple[str, str]]:
    workspace = payload["workspace"]
    order_range = payload["order_range"]
    return [
        ("Name", workspace["name"] or "—"),
        ("Workspace", workspace["id"]),
        ("Created", payload["created_at"]),
        ("Config", payload["config"]),
        ("Generator", payload["generator"] or "—"),
        ("Graphs", str(payload["graphs"])),
        (
            "Order range",
            f"{order_range['min']}..{order_range['max']}"
            if order_range["min"] is not None
            else "—",
        ),
        ("Lines", str(payload["lines"])),
        ("Database", payload["database"]),
        ("Disk usage", _format_bytes(payload["disk_bytes"])),
    ]


def _render_line_list(payload: dict[str, Any]) -> None:
    workspace = payload["workspace"]
    workspace_label = workspace["name"] or workspace["id"]
    _CONSOLE.print(f"[bold]Workspace:[/bold] {workspace_label} ({workspace['id']})")
    if not payload["lines"]:
        _CONSOLE.print(f"No lines in workspace {workspace_label}.")
        return
    table = Table(box=None)
    table.add_column("ID")
    table.add_column("CREATED")
    table.add_column("GRAPHS", justify="right")
    table.add_column("LATEST")
    for line in payload["lines"]:
        table.add_row(
            line["id"],
            _format_created_at(line["created_at"]),
            str(line["graphs"]),
            "*" if line["latest"] else "",
        )
    _CONSOLE.print(table)


def _format_created_at(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def _render_line_status(payload: dict[str, Any]) -> None:
    line_label = payload["line"]["id"]
    if payload["selected_latest"]:
        workspace = payload["workspace"]
        workspace_label = workspace["name"] or workspace["id"]
        line_label = f"{line_label} (latest in workspace {workspace_label})"
    _status_table(
        [
            ("Line", line_label),
            ("Workspace", payload["workspace"]["id"]),
            ("Graphs", str(payload["graphs"])),
            ("Created", payload["created_at"]),
            ("Phase", payload["phase"]),
            ("Database", payload["database"]),
        ]
    )


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
