# Graphoratory CLI Foundation

This document records the implemented CLI and persistence foundation. It describes the
current code, not future milestones.

## 1. Implemented functionality

Graphoratory currently provides a Typer-based `graphlab` CLI with three command groups:
`workspace`, `graph`, and `line`. Typer owns command parsing, positional arguments, command
groups, and `--help`. Trailing `key=value` tokens are parsed only after Typer has selected a
command.

The implemented operations are:

- load `experiment.toml` from the current directory, or another file selected with
  `config=PATH`;
- create a named workspace with an immutable canonical `ws-xxxxxxxx` identity;
- select a workspace explicitly or through `workspace.active`;
- list workspace names, canonical IDs, creation times, and the configured active workspace;
- show read-only workspace status;
- rebuild a workspace SQLite index from authoritative artifacts;
- generate and persist one graph corpus in a workspace;
- create a line with a fixed graph subset;
- show read-only status for an explicitly selected line;
- resolve lowercase typed workspace, line, and graph IDs;
- index workspace, graph, line, and line-membership data in SQLite;
- keep filesystem manifests and graph data authoritative;
- apply strict `key=value` configuration overrides and reject unknown keys.

Policy generation, policy evaluation, policy repair, policy mutation, baselines, branching,
`line next`, `line run`, scheduling, counterexample search, Codex App Server integration,
and dashboards are not implemented in this milestone.

## 2. Configuration

The default configuration file is `experiment.toml` in the current working directory. Its
implemented schema is:

```toml
[workspace]
root = "workspaces"
active = "test01"

[graphs]
mode = "unrestricted_min_degree_3"
workspace_graph_count = 1000
line_graph_count = 100
min_order = 22
max_order = 63
seed = 401
```

The keys mean:

- `workspace.root`: workspace storage directory, resolved relative to the configuration file;
- `workspace.active`: optional workspace name or lowercase typed workspace ID used by
  commands that need a workspace;
- `graphs.mode`: implemented graph-generation mode;
- `graphs.workspace_graph_count`: number of graphs persisted in the workspace corpus;
- `graphs.line_graph_count`: number of corpus graphs selected for a new line;
- `graphs.min_order` and `graphs.max_order`: inclusive graph-order range;
- `graphs.seed`: non-negative generation seed.

Unknown sections, keys, and override keys fail. The obsolete top-level `active_workspace`,
`graphs.count`, and `graphs.line_sample_size` keys are not accepted.

## 3. Commands

The implemented command forms are:

```text
graphlab --help
graphlab workspace --help
graphlab workspace init NAME [key=value ...]
graphlab workspace list [key=value ...]
graphlab workspace status [WORKSPACE] [key=value ...]
graphlab workspace reindex [WORKSPACE] [key=value ...]
graphlab graph generate [key=value ...]
graphlab line create [key=value ...]
graphlab line status LINE [key=value ...]
```

Examples:

```bash
graphlab workspace init test01
graphlab workspace list
graphlab workspace status
graphlab workspace status test01
graphlab workspace status ws-a1b2c3d4
graphlab graph generate
graphlab graph generate workspace=test01 graphs.workspace_graph_count=100
graphlab line create
graphlab line create workspace=test01 graphs.line_graph_count=20
graphlab line status ln-38aa192f
graphlab workspace reindex
```

There is no `graphlab workspace path` command.

## 4. Workspace selection

Commands that need a workspace use this exact precedence:

1. an explicit command target;
2. `workspace.active` from the effective configuration;
3. an error explaining how to select a workspace.

`workspace status` and `workspace reindex` accept a positional workspace name or typed ID.
`graph generate` and `line create` accept the operational `workspace=<name-or-id>` target.
An explicit target takes precedence over `workspace.active`.

The implementation never guesses the first, latest, oldest, or only workspace.

`line status` always requires an explicit lowercase typed line ID. There is no active-line
setting and no implicit line selection.

## 5. Workspace names and filesystem layout

A workspace has both:

- a human name such as `test01`;
- a canonical hash identity such as `ws-a1b2c3d4`.

The canonical directory remains:

```text
workspaces/ws-a1b2c3d4/
```

For a named workspace, Graphoratory also creates a relative, rebuildable filesystem alias:

```text
workspaces/test01 -> ws-a1b2c3d4
```

This makes the human name visible during ordinary directory inspection without replacing
the canonical identity. `workspace reindex` recreates a missing alias from the authoritative
workspace manifest. The symlink target is relative and does not encode the checkout path.

Workspace names are 1 to 64 characters, start with an ASCII letter or digit, and then use
only ASCII letters, digits, `_`, or `-`. Path separators, traversal names, typed workspace
IDs used as names, and duplicate names are rejected.

The authoritative layout is:

```text
workspaces/
  NAME -> ws-xxxxxxxx
  ws-xxxxxxxx/
    manifest.json
    index.sqlite3
    graphs/
      manifest.json
      graphs.jsonl.gz
    lines/
      ln-xxxxxxxx/
        manifest.json
```

## 6. Typed identifiers

Implemented human-facing typed IDs are:

```text
ws-a1b2c3d4
ln-38aa192f
gr-b782c0e1
```

Full 64-character lowercase hashes are authoritative internally. Short IDs contain the first
eight lowercase hexadecimal characters. CLI resolution accepts supported typed short or full
IDs, rejects the wrong type or malformed casing, and fails on ambiguous short hashes.

The documented `cp-xxxxxxxx` namespace is reserved, but the current corpus is stored directly
under a workspace and no separate corpus entity or directory is implemented.

## 7. Authoritative artifacts

Filesystem artifacts are authoritative. SQLite is a rebuildable projection.

The workspace manifest stores its semantic identity, human name, creation time, and graph
creation configuration. It does not store the absolute configuration path or workspace-root
path. Graph manifests store generation parameters and graph hashes. Line manifests store the
line hash, full workspace hash, creation time, and selected full graph hashes.

Persisted relationships use hashes instead of filesystem paths. No authoritative mutable
state file is used. Workspace, graph, and line artifacts are published atomically.

`workspace status` and `line status` are read-only. They do not edit configuration, repair
artifacts, generate data, or rebuild SQLite. A projection mismatch is reported as requiring
reindexing.

## 8. SQLite and reindexing

Each canonical workspace directory contains `index.sqlite3`. SQLite indexes:

- the full and short workspace hashes and unique human name;
- full and short graph hashes, workspace ownership, and graph order;
- full and short line hashes, workspace ownership, creation time, and graph count;
- ordered line-to-graph membership using full hashes.

Migration `0002_workspace_name` added the unique workspace-name projection. Migration
`0003_portable_persistence` removes the redundant `manifest_path` columns from `workspaces`
and `lines`. SQLite therefore persists semantic IDs and metadata, not machine-specific
absolute artifact paths.

`workspace reindex` reads the workspace, graph, and line artifacts, validates their hashes and
counts, constructs a new database, verifies SQLite integrity, and atomically replaces the old
projection. It also removes obsolete absolute-path provenance fields from older manifests and
recreates the relative human-name alias.

Deleting SQLite and running `graphlab workspace reindex` reconstructs workspace identity and
name, graphs, lines, and line memberships from the filesystem artifacts.

## 9. Override syntax

Normal CLI arguments and options remain normal Typer syntax. Additional configuration
overrides use dotlist assignments:

```bash
graphlab graph generate graphs.workspace_graph_count=100
graphlab line create graphs.line_graph_count=20
graphlab graph generate workspace.active=test01
graphlab graph generate workspace=test02
graphlab workspace list config=other-experiment.toml
```

`workspace.active=test01` changes the effective configuration for that invocation.
`workspace=test02` is a command-scoped explicit target and therefore takes precedence over
the configured active workspace. `config=PATH` selects the configuration file. Unknown keys,
empty values, malformed assignments, and duplicate assignments fail clearly.
