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
- generate and persist one graph corpus using a concrete generator or weighted mix;
- create a line with a fixed graph subset;
- list lines in a selected workspace from authoritative manifests;
- show read-only status for an explicit line or the latest line in the selected workspace;
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

The keys mean:

- `workspace.root`: workspace storage directory, resolved relative to the configuration file;
- `workspace.active`: optional workspace name or lowercase typed workspace ID used by
  commands that need a workspace;
- `graphs.generator`: concrete generator name or `mixed`;
- `graphs.workspace_graph_count`: number of graphs persisted in the workspace corpus;
- `graphs.line_graph_count`: number of corpus graphs selected for a new line;
- `graphs.min_order` and `graphs.max_order`: inclusive graph-order range;
- `graphs.seed`: non-negative root generation seed;
- `graphs.random_regular.degree_min` and `degree_max`: allowed regular degrees;
- `graphs.erdos_renyi_rejection.expected_degree_min` and `expected_degree_max`:
  expected-degree interval converted to `p = expected_degree / (n - 1)`;
- `graphs.degree_sequence_rejection.degree_min` and `degree_max`: target degree interval;
- `graphs.mixed.generators` and `weights`: concrete generator names and matching positive
  dispatcher weights.

The implemented generator names are:

```text
cycle_matching_stub_pairing
random_regular
erdos_renyi_rejection
degree_sequence_rejection
mixed
```

`cycle_matching_stub_pairing` preserves the original Graphoratory/HEG algorithm: even
orders use a Hamiltonian cycle plus a random non-cycle perfect matching; odd orders use
bounded greedy stub pairing for the degree sequence `(4, 3, ..., 3)`.
`random_regular` uses NetworkX simple random regular generation and rejects disconnected
results. `erdos_renyi_rejection` draws an unmodified `G(n,p)` candidate and rejects it
unless it satisfies the common invariant. `degree_sequence_rejection` samples a
heterogeneous target degree sequence, checks graphicality, realizes it with Havel–Hakimi,
applies degree-preserving double-edge swaps, and rejects invalid results. `mixed` only
selects among configured concrete generators.

Unknown sections, keys, generator names, and override keys fail. The obsolete top-level
`active_workspace`, `graphs.mode`, `graphs.count`, and `graphs.line_sample_size` keys are
not accepted by configuration loading.

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
graphlab line list [key=value ...]
graphlab line status [LINE] [key=value ...]
```

The implemented line command tree is:

```text
graphlab line
├── create
├── list
└── status
```

Every leaf command also accepts `--json`. The normal console output remains Rich-formatted
for humans. Internally, each command first builds one JSON-serializable semantic payload;
the Rich renderer formats that same payload, while `--json` writes it directly without
tables, ANSI styling, or human-only unit formatting.

JSON examples:

```bash
graphlab workspace list --json
graphlab workspace status --json
graphlab graph generate --json
graphlab line list workspace=test02 --json
graphlab line status --json
```

JSON identifiers contain both the typed short `id` and authoritative full `hash`. Numeric
values and booleans remain JSON numbers and booleans. Status payloads expose raw values such
as `disk_bytes`, structured `order_range`, and `selected_latest`; list payloads contain
arrays of complete displayed rows. Successful JSON output is written to stdout. Application
errors and Typer parsing errors in JSON mode are written to stderr as:

```json
{"error": {"message": "...", "type": "ArtifactError"}}
```

This includes missing required arguments, unknown options, and unknown commands, even though
those failures occur before an application handler runs. Standard invocations without
`--json` retain Typer/Rich error rendering. `--help` remains the normal human-readable Typer
help and exits successfully.

Normal Rich-output examples:

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
graphlab line list
graphlab line list workspace=test02
graphlab line status
graphlab line status ln-38aa192f
graphlab line status workspace=test01
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

## 4.1 Implicit latest-line selection for manual commands

`line status` accepts an optional lowercase typed line ID. Its selection precedence is:

```text
explicit LINE
→ latest line in the selected workspace
→ error if the workspace has no lines
```

Without `LINE`, the workspace is selected by the ordinary explicit `workspace=...` then
`workspace.active` precedence. Latest means the greatest parsed UTC `created_at` timestamp
from immutable line manifests. An exact timestamp tie is resolved by descending full line
hash. Directory order, filesystem timestamps, SQLite insertion order, and line-ID
lexicographic order do not determine recency.

This is a visible convenience for interactive CLI use: output labels an implicit choice as
`latest in workspace NAME`. There is no `active_line`, line configuration section, current
line file, or mutable selection state. SQLite is not required for latest-line resolution.
Automated and internal execution should continue to pass a concrete line ID.

When an explicit line is used together with configured or explicit workspace context, the
line must belong to that workspace. A mismatch fails instead of switching workspaces.

## 4.2 Line listing

`graphlab line list` lists lines from the selected workspace. The workspace follows the
ordinary precedence:

```text
explicit workspace=...
→ workspace.active
→ error
```

The compact Rich output identifies the workspace once and contains exactly these columns:

```text
ID | CREATED | GRAPHS | LATEST
```

Rows are ordered by parsed UTC `created_at` descending, then full line hash descending for
an exact timestamp tie. This is the same ordering used by implicit latest-line selection,
so the first row is marked `*` in `LATEST`. The marker is derived display metadata, not
active-line state.

Listing is read-only and reconstructed from authoritative line manifests. It does not
require SQLite, reindex artifacts, or change configuration. An empty workspace exits
successfully with `No lines in workspace NAME.`

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
path. Graph manifests store the selected generator, generator parameters, root seed, order
range, requested and actual distinct counts, ordered graph hashes, attempted/rejected/
duplicate candidate counts, and accepted counts per concrete generator. Line manifests
store the line hash, full workspace hash, creation time, and selected full graph hashes.

For every candidate attempt, Graphoratory derives a local RNG seed from SHA-256 of the root
seed and attempt number. That RNG samples the candidate order uniformly over the inclusive
range, selects a mixed component when needed, and drives candidate construction. The same
supported software version and effective generation configuration therefore reproduce the
same ordered graph hashes. Invalid and duplicate candidates do not count toward
`workspace_graph_count`; generation fails after
`max(1000, workspace_graph_count * 100)` attempts if the distinct target cannot be reached.

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

Migration `0004_graph_corpus_generator` adds one `graph_corpora` projection row per generated
workspace corpus. It stores the selected generator, compact canonical JSON configuration,
requested and actual graph counts, and aggregate attempt/rejection statistics. Reindexing
reconstructs this row from the graph manifest.

`workspace reindex` reads the workspace, graph, and line artifacts, validates their hashes and
counts, constructs a new database, verifies SQLite integrity, and atomically replaces the old
projection. It also removes obsolete absolute-path provenance fields from older manifests and
recreates the relative human-name alias. After success it renders a Rich report containing
the workspace name and ID, creation time, portable configuration reference, selected
generator, graph and line counts, order range, indexed database state, and disk usage.
Project-relative references use notation such as `$PROJECT/experiment.toml`; the report
does not print the absolute configuration or checkout path. `$PROJECT` is the command
invocation directory when the selected config is inside it; for an external config it is
the config file's containing directory. Configuration-loading errors use the same notation.

Deleting SQLite and running `graphlab workspace reindex` reconstructs workspace identity and
name, graphs, lines, and line memberships from the filesystem artifacts.

## 9. Override syntax

Normal CLI arguments and options remain normal Typer syntax. Additional configuration
overrides use dotlist assignments:

```bash
graphlab graph generate graphs.workspace_graph_count=100
graphlab graph generate graphs.generator=random_regular
graphlab graph generate \
  graphs.generator=random_regular \
  graphs.random_regular.degree_min=3 \
  graphs.random_regular.degree_max=4
graphlab graph generate graphs.generator=erdos_renyi_rejection
graphlab graph generate graphs.generator=mixed
graphlab line create graphs.line_graph_count=20
graphlab graph generate workspace.active=test01
graphlab graph generate workspace=test02
graphlab workspace list config=other-experiment.toml
```

`workspace.active=test01` changes the effective configuration for that invocation.
`workspace=test02` is a command-scoped explicit target and therefore takes precedence over
the configured active workspace. `config=PATH` selects the configuration file. Unknown keys,
empty values, malformed assignments, and duplicate assignments fail clearly.
