# Milestone 2 — Workspace Graphs, Lines, and SQLite Index Foundation

**Status:** COMPLETE  
**Repository:** `hipotures/graphoratory`  
**Completion commit:** `67d802f05022a99135c452ff905a2cd56975cd4b`  
**Completion CI:** GitHub Actions run `31909219861` — success

## Purpose

Milestone 2 closes the operational foundation required before scientific policy evaluation begins.

The project now has stable workspace isolation, persistent graph sets, fixed line graph subsets, rebuildable workspace-local SQLite indexes, SQL-first intra-workspace lookup, human and machine-readable CLI output, and CI guards that enforce the persistence architecture.

This milestone deliberately stops before baseline evaluation, policy execution, AI generation, mutation, branching, or counterexample search.

---

## Completed command surface

The implemented CLI foundation is:

```text
graphlab workspace init NAME
graphlab workspace list
graphlab workspace status [WORKSPACE]
graphlab workspace reindex [WORKSPACE]

graphlab graph generate

graphlab line create
graphlab line list
graphlab line status [LINE]
```

Every leaf command supports `--json` in addition to the default Rich output.

Manual line commands may use the latest line in the selected workspace when `LINE` is omitted. This is a derived lookup and does not introduce mutable active-line state.

---

## Workspace model

A workspace is the top-level isolation unit for one persistent graph set and its search lines.

Canonical workspace directories use typed IDs:

```text
workspaces/
├── ws-a1b2c3d4/
├── ws-b2c3d4e5/
└── ...
```

Human workspace names are represented by relative symlink aliases:

```text
workspaces/test01 -> ws-a1b2c3d4
```

The canonical directory remains the typed workspace directory.

### Workspace discovery

`graphlab workspace list` is the intentional project-level filesystem exception.

It performs only a shallow enumeration of the immediate `workspaces/` directory, selects canonical `ws-*` workspace directories, and reads their small workspace manifests.

It does **not** recursively scan graph or line artifacts.

Symlink aliases are not treated as separate workspaces.

The current list fields are derived as follows:

```text
NAME     <- workspace manifest
ID       <- workspace hash from the workspace manifest
CREATED  <- workspace manifest
ACTIVE   <- experiment.toml workspace.active
```

There is intentionally no persistent project-wide SQLite catalog.

---

## SQLite architecture

Each workspace owns exactly one rebuildable SQLite database:

```text
workspaces/ws-a1b2c3d4/index.sqlite3
```

There is no project-root SQLite database and no second SQLite catalog layer.

The durable rule is:

> Filesystem artifacts are authoritative. SQLite is a rebuildable workspace-local index, locator, projection, and query layer.

A workspace database currently contains the tables:

```text
workspaces
graph_corpora
graphs
lines
line_graphs
```

`graph_corpora` is an internal schema/table name for graph-generation metadata. It does not introduce a first-class user-facing corpus object or `cp-*` identity.

SQLite contains compact metadata, identities, relationships, and lookup information. Large scientific payloads remain in filesystem artifacts.

---

## SQL-first intra-workspace lookup

Once a workspace has been selected, normal entity lookup must use that workspace's SQLite index.

Examples:

```text
line list
latest line
explicit line resolution
line graph membership
workspace projection counts
```

These operations query:

```text
workspaces/ws-xxxxxxxx/index.sqlite3
```

They do not enumerate `lines/` or search manifests to locate an entity.

The latest-line ordering is:

```sql
ORDER BY created_at DESC, line_hash DESC
LIMIT 1
```

This ordering is shared by latest-line selection and line listing.

When a specific authoritative artifact is needed after SQL lookup, the exact path is derived from known semantic IDs and only that artifact is read.

A missing or stale workspace index does not trigger a silent filesystem fallback. Normal intra-workspace operations fail with a reindex diagnostic instead.

---

## Reindex behavior

`graphlab workspace reindex [WORKSPACE]` rebuilds **exactly one selected workspace database**.

It does not scan or rebuild unrelated workspaces.

The reindex operation is allowed to scan the selected workspace's authoritative artifacts because reconstruction is its explicit purpose.

The rebuild sequence is conceptually:

```text
selected workspace artifacts
        ↓
new temporary SQLite database
        ↓
index workspace, graphs, lines, memberships
        ↓
PRAGMA integrity_check
PRAGMA foreign_key_check
        ↓
atomic replacement of workspace index.sqlite3
```

If filesystem publication of a new artifact succeeds but subsequent SQLite indexing fails, the filesystem artifact remains authoritative and the user is instructed to reindex the workspace.

---

## Persistent graph set

`graphlab graph generate` creates the persistent graph set owned by the selected workspace.

Current graph configuration uses:

```toml
[graphs]
generator = "mixed"
workspace_graph_count = 1000
line_graph_count = 100
min_order = 22
max_order = 63
seed = 401
```

The implementation supports the validated generator families:

```text
cycle_matching_stub_pairing
random_regular
erdos_renyi_rejection
degree_sequence_rejection
mixed
```

Generated graphs satisfy the current structural baseline:

- simple;
- undirected;
- connected;
- minimum degree at least three;
- order within the configured inclusive range.

Distinct graphs are identified by stable content hashes. Duplicate candidates do not count toward `workspace_graph_count`.

The graph payload remains packed in compressed JSON Lines rather than one file per graph.

---

## Line semantics

A line is one independent future policy-search trajectory.

At creation time it selects one fixed subset of graph hashes from its workspace graph set.

That subset is immutable for the lifetime of the line.

Different lines in the same workspace may select different subsets.

Example current state:

```text
Workspace: test01 (ws-f85b931e)
 ID           CREATED                  GRAPHS  LATEST
 ln-784d8191  2026-08-15 20:00:15 UTC     100  *
 ln-06c21d25  2026-08-15 20:00:12 UTC     100
```

And:

```text
Line         ln-784d8191 (latest in workspace test01)
Workspace    ws-f85b931e
Graphs       100
Created      2026-08-15T20:00:15.139637Z
Phase        ready for policy generation
Database     indexed
```

This is the intended terminal state for Milestone 2.

---

## Persistence invariants

The following invariants are now binding:

1. Filesystem artifacts are authoritative.
2. There is one rebuildable `index.sqlite3` per workspace.
3. There is no persistent project-wide SQLite index.
4. Normal intra-workspace lookup is SQL-first.
5. Missing/stale SQLite does not silently fall back to deep filesystem scans.
6. Reindex is explicitly allowed to scan the selected workspace artifacts.
7. `workspace list` may perform only shallow top-level workspace discovery.
8. No authoritative mutable `state.json`, checkpoint, or active-line file exists.
9. Persisted project-owned paths are portable/project-relative or derived from semantic IDs.
10. Completed artifacts are published atomically.

---

## Automated architecture guards

`tests/test_architecture.py` protects the storage model against regression.

It inspects the production source tree and restricts filesystem enumeration to explicit boundaries.

The currently permitted production enumeration sites are:

```text
application._directory_size           -> rglob, only for explicit disk-usage measurement
artifacts.scan_workspace_directories  -> shallow top-level workspace discovery
artifacts.scan_line_artifacts         -> reindex/recovery only
```

The architecture tests also assert that the application does not use the project root as SQLite scope and that normal application code does not call the line-artifact scanner.

A future accidental `glob`, `rglob`, or `iterdir` in an unapproved production path therefore fails CI instead of silently changing the architecture.

---

## Machine-readable CLI

All implemented leaf commands expose `--json`.

The CLI constructs one semantic payload and renders either JSON or Rich output from that payload.

Important properties:

- typed short IDs remain human-friendly;
- full hashes are present in JSON where applicable;
- numbers remain numeric;
- booleans remain booleans;
- timestamps remain full values;
- application and parser errors are structured JSON when `--json` is requested;
- default non-JSON behavior remains normal Typer/Rich output.

This is intended to support both manual research use and future automated orchestration without creating a separate internal CLI protocol.

---

## Verification at milestone close

The final correction was merged as:

```text
67d802f05022a99135c452ff905a2cd56975cd4b
Restore per-workspace SQLite indexes
```

The completion GitHub Actions run was:

```text
31909219861
```

Result:

```text
pytest:     passed
Ruff:       passed
mypy:       passed
CLI smoke:  passed
CI:         success
```

The test suite at this checkpoint contains 86 passing tests.

---

## Explicitly not implemented in this milestone

Milestone 2 does not implement:

- baseline policy evaluation;
- the scientific evaluator;
- custom Python policy execution;
- Codex App Server integration;
- AI policy generation;
- mutation;
- repair loops;
- branch search;
- `line next`;
- `line run`;
- scheduler/orchestrator;
- counterexample search;
- dashboard.

No part of those later stages should be inferred from this milestone's infrastructure.

---

## Next milestone

The next scientific milestone is **baseline + evaluator**.

The required flow is:

```text
fixed line graph subset
        ↓
known baseline algorithm
        ↓
independent evaluator
        ↓
score + minimal diagnostics + resource metrics
```

The baseline must be taken from the scientifically validated legacy HEG / Mutation Forge Lab implementation where appropriate.

The evaluator must preserve the actual existing scientific score semantics rather than inventing a replacement metric.

The baseline is used first to calibrate and verify evaluator correctness.

Only after baseline evaluation is correct should the project proceed to custom policy evaluation and later AI-generated policy search.

---

## Milestone decision

**Milestone 2: COMPLETE.**

The workspace / graph / line persistence foundation is considered closed unless a concrete defect is discovered.

Further architecture work should now serve the baseline-and-evaluator milestone rather than continue expanding this foundation.