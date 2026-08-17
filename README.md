# graphoratory

Graphoratory is a small Python 3.12 application for creating workspaces, generating
validated workspace graphs, and assigning a fixed graph subset to an independent line.
The `graphlab` command is intentionally thin; its operations call reusable application
services.

## Current Erdős–Gyárfás results

The repository is also being used as an experimental laboratory for the
Erdős–Gyárfás conjecture. For a graph order `n`, define

\[
F(n) = \min_G \sum_{2^k \le n} C_{2^k}(G),
\]

where the minimum is taken over all simple, connected `n`-vertex graphs with minimum
degree at least three, and `C_l(G)` is the number of cycles of length `l`.
A counterexample to the conjecture would have `F(n) = 0` for some `n`.

For heuristic searches, the table reports `T(n)`, the smallest value found so far.
Therefore `T(n)` is only an upper bound on `F(n)`. The only values currently certified
exact are `F(10)` and `F(11)`, obtained by exhaustive non-isomorphic generation with
`nauty/geng` and exact cycle counting. Profiles are listed in increasing forbidden-cycle
length order: `(C4,C8)` below 16, `(C4,C8,C16)` for 16--31, and
`(C4,C8,C16,C32)` for 32--39.

| n | best known value | profile | status |
|---:|---:|---|---|
| 10 | **4** | `(4,0)` | **certified exact: F(10)=4** |
| 11 | **2** | `(2,0)` | **certified exact: F(11)=2** |
| 12 | 3 | `(3,0)` | heuristic upper bound |
| 13 | 2 | `(2,0)` | heuristic upper bound |
| 14 | 2 | `(2,0)` | heuristic upper bound |
| 15 | 3 | `(3,0)` | heuristic upper bound |
| 16 | 3 | `(3,0,0)` | heuristic upper bound |
| 17 | 3 | `(3,0,0)` | heuristic upper bound |
| 18 | 3 | `(3,0,0)` | heuristic upper bound |
| 19 | 3 | `(3,0,0)` | heuristic upper bound |
| 20 | 3 | `(3,0,0)` | heuristic upper bound |
| 23 | 3 | `(3,0,0)` | heuristic upper bound |
| 24 | 3 | `(3,0,0)` | heuristic upper bound |
| 25 | 3 | `(3,0,0)` | heuristic upper bound |
| 26 | 3 | `(3,0,0)` | heuristic upper bound |
| 27 | 3 | `(3,0,0)` | heuristic upper bound |
| 28 | 3 | `(3,0,0)` | heuristic upper bound |
| 29 | 3 | `(3,0,0)` | heuristic upper bound |
| 30 | 4 | `(4,0,0)` | heuristic upper bound |
| 31 | 4 | `(3,1,0)` | heuristic upper bound |
| 32 | 4 | `(3,1,0,0)` | heuristic upper bound |
| 33 | 4 | `(4,0,0,0)` | heuristic upper bound |
| 34 | **3** | `(3,0,0,0)` | heuristic upper bound |
| 35 | 4 | `(4,0,0,0)` | heuristic upper bound |
| 36 | 4 | `(3,1,0,0)` | heuristic upper bound |
| 37 | 4 | `(3,1,0,0)` | heuristic upper bound |
| 38 | 4 | `(3,1,0,0)` | heuristic upper bound |
| 39 | 4 | `(4,0,0,0)` | heuristic upper bound |

The exhaustive searches inspected 5,203,110 non-isomorphic admissible graphs for
`n=10` and 577,076,528 for `n=11`. No graph with total 0 or 1 exists at order 11;
the exact minimum is two 4-cycles.

The current high-order search uses a cycle-blind legal ADD/REMOVE random walk with
alternating RANDOM and ELITE parent phases. Candidate scoring is cascaded through
`C4 -> C8 -> C16 -> C32`, conservatively pruning a candidate as soon as its proven
partial total cannot beat the incumbent. This avoids almost all expensive `C32`
computations while preserving correctness of the comparison against the incumbent.
The bounded-memory implementation does not retain hashes for every rejected candidate,
so long runs remain stable instead of accumulating tens of millions of Python set
entries.

The strongest result in the currently explored `n >= 32` range is
`T(34) <= 3` with profile `(3,0,0,0)`. At order 32, two distinct total-4 profile classes
have been observed: `(3,1,0,0)` and `(4,0,0,0)`; the former is preferred by the current
weighted tie-break but the latter is also repeatedly reachable. Orders 21 and 22 have
not yet been included in these calibration/search campaigns.

**No Erdős–Gyárfás counterexample has been found.** Except for `n=10` and `n=11`, the
numbers above are constructive upper bounds only and must not be read as exact values
of `F(n)`.

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
After success it prints a Rich summary of the rebuilt workspace instead of only its ID.
Status commands inspect but never repair data.

The graph seed construction and structural checks were adapted from
`sglab.targets.erdos_gyarfas.ErdosGyarfasPlugin.generate_seed` and `validate_graph` in the
read-only HEG reference repository. Graphoratory uses its own deterministic normalized
edge JSON hashing. The core Graphoratory application does not include HEG search,
mutation, scoring, campaign, or web subsystems; the repository's research scripts and
result artifacts are maintained separately from the core application services.
