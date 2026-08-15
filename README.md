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
`graphs.workspace_graph_count=20`. Unknown keys fail.

The default configuration is:

```toml
[workspace]
root = "workspaces"
active = "test01"

[graphs]
generator = "mixed"
workspace_graph_count = 1000
line_graph_count = 100
min_order = 22
max_order = 63
seed = 401

[graphs.random_regular]
degree_min = 3
degree_max = 6

[graphs.erdos_renyi_rejection]
expected_degree_min = 6.0
expected_degree_max = 10.0

[graphs.degree_sequence_rejection]
degree_min = 3
degree_max = 10

[graphs.mixed]
generators = [
    "cycle_matching_stub_pairing",
    "random_regular",
    "erdos_renyi_rejection",
    "degree_sequence_rejection",
]
weights = [1.0, 1.0, 1.0, 1.0]
```

Candidate graph orders are sampled uniformly across the inclusive configured range.
Available generators are `cycle_matching_stub_pairing`, `random_regular`,
`erdos_renyi_rejection`, `degree_sequence_rejection`, and the weighted `mixed`
dispatcher. Every accepted graph is simple, undirected, connected, and has minimum
degree at least three.

## Commands

```bash
uv run graphlab workspace init testowy
uv run graphlab workspace list
uv run graphlab graph generate \
  graphs.generator=random_regular \
  graphs.workspace_graph_count=20
uv run graphlab workspace status
uv run graphlab workspace status testowy
uv run graphlab line create graphs.line_graph_count=5
uv run graphlab line status ln-xxxxxxxx
uv run graphlab workspace reindex
```

Commands that need a workspace use an explicit `workspace=<name-or-id>` first, then
`workspace.active`; they fail rather than guessing if neither is set. `line status`
always requires an explicit line. A workspace accepts one immutable graph generation;
a second `graph generate` fails instead of overwriting it.

Each named workspace keeps its canonical `workspaces/ws-xxxxxxxx/` directory and exposes
a relative human-readable alias such as `workspaces/testowy -> ws-xxxxxxxx`. The alias is
recreated by `workspace reindex`; persisted manifests and SQLite rows contain semantic
hashes rather than absolute checkout paths.

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
├── testowy -> ws-xxxxxxxx
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
