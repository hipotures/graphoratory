# graphoratory

Graphoratory is a small Python 3.12 application for creating workspaces, generating
validated workspace graphs, and assigning a fixed graph subset to an independent line.
The `graphlab` command is intentionally thin; its operations call reusable application
services.

## Setup

Use [uv](https://docs.astral.sh/uv/) for all environment and package operations:

```bash
uv sync
uv run graphlab --help
```

Every command loads `experiment.toml` from the current directory unless given
`config=/path/to/file.toml`. Normal command targets use positional arguments. Additional
configuration uses `key=value` overrides, including nested settings such as
`graphs.count=20`. Unknown keys fail.

The default configuration is:

```toml
active_workspace = "testowy"

[workspace]
root = "workspaces"

[graphs]
mode = "unrestricted_min_degree_3"
count = 1000
line_sample_size = 100
min_order = 22
max_order = 63
seed = 401
```

Graph orders are assigned deterministically in round-robin order across the inclusive
configured range. Per-attempt seeds are derived from the root seed, attempt number, and
order. Even orders use HEG's cycle-plus-perfect-matching cubic construction. Odd orders
use HEG's mixed-degree construction.

## Commands

```bash
uv run graphlab workspace init testowy
uv run graphlab workspace list
uv run graphlab graph generate graphs.count=20
uv run graphlab workspace status
uv run graphlab workspace status testowy
uv run graphlab line create graphs.line_sample_size=5
uv run graphlab line status ln-xxxxxxxx
uv run graphlab workspace reindex
```

Commands that need a workspace use an explicit `workspace=<name-or-id>` first, then
`active_workspace`; they fail rather than guessing if neither is set. `line status`
always requires an explicit line. A workspace accepts one immutable graph generation;
a second `graph generate` fails instead of overwriting it.

Typed short IDs and artifact directory names are always lowercase:

```text
ws-xxxxxxxx  workspace
gr-xxxxxxxx  graph
ln-xxxxxxxx  line
```

The full lowercase SHA-256 hash is authoritative inside manifests and SQLite.

## Artifacts and SQLite

```text
workspaces/
└── ws-xxxxxxxx/
    ├── manifest.json
    ├── index.sqlite3
    ├── graphs/
    │   ├── manifest.json
    │   └── graphs.jsonl.gz
    └── lines/
        └── ln-xxxxxxxx/
            └── manifest.json
```

Completed filesystem artifacts are immutable and authoritative. Graph records are
packed into gzip-compressed JSON Lines. SQLite is only a query projection. If it is absent
or inconsistent, `workspace reindex` reconstructs the workspace, graphs, lines,
and line membership from artifacts and checks the rebuilt database before publication.
Status commands inspect but never repair data.

The graph seed construction and structural checks were adapted from
`sglab.targets.erdos_gyarfas.ErdosGyarfasPlugin.generate_seed` and `validate_graph` in the
read-only HEG reference repository. Graphoratory uses its own deterministic normalized
edge JSON hashing and does not include HEG search, mutation, scoring, campaign, or web
subsystems.
