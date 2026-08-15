# SQLite index architecture

Graphoratory uses **one rebuildable SQLite index per workspace**.

Canonical layout:

```text
workspaces/
  ws-xxxxxxxx/
    index.sqlite3
    manifest.json
    graphs/
    lines/
```

There is no project-wide SQLite database and no second catalog database.

## Authority

Immutable filesystem artifacts are authoritative. SQLite is derived and may always be rebuilt from the artifacts of its own workspace.

SQLite stores compact identity, ownership, ordering, graph metadata, and line membership needed to locate authoritative data. Large scientific payloads remain in files.

Completed baseline evaluations are immutable files below their exact line directory.
SQLite projects their identity, ownership, baseline, graph count, and exact rational score
endpoints in the workspace-local `evaluations` table.

## Normal lookup

After a workspace is selected, ordinary lookup inside that workspace is SQL-first:

```text
workspace/index.sqlite3
→ identify line/graph/relation
→ derive the one exact artifact path
→ read that exact file only if its full payload is needed
```

Normal line lookup, `line list`, latest-line selection, line membership, and indexed graph metadata must not enumerate `lines/` or search artifact files.

If the workspace index is missing or stale, ordinary intra-workspace operations fail with a reindex diagnostic. They do not silently scan artifacts.

## Workspace discovery

No global database exists. Therefore shallow enumeration of the top-level `workspaces/ws-*` directory is allowed for `workspace list`, duplicate workspace-name checks, and explicit recovery. Workspace names normally resolve through their relative symlink aliases; typed workspace IDs derive their canonical directory directly.

This exception does not permit recursive or intra-workspace artifact discovery during ordinary operations.

## Reindex

```bash
graphlab workspace reindex [WORKSPACE]
```

rebuilds **only the selected workspace's** `index.sqlite3`.

Reindex is the explicit operation allowed to enumerate that workspace's graph and line artifacts. It validates them, builds a new temporary SQLite database, runs SQLite integrity and foreign-key checks, and atomically replaces the selected workspace index.

This boundary also permits enumerating completed evaluation artifacts under the already
identified line artifacts. Ordinary baseline evaluation does not enumerate those
directories.

Reindexing workspace A must not read, rebuild, delete, or modify workspace B's SQLite database.

## CI architecture guard

`tests/test_architecture.py` is a deliberate architectural gate. It fails CI if new production filesystem enumeration (`iterdir`, `glob`, `rglob`) appears outside the small allowlist of explicit boundaries, or if application code starts using the project root as SQLite scope.

Any future exception must be justified as an architectural decision and added explicitly to that allowlist; do not weaken the test merely to make a new filesystem scan pass.
