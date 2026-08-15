Work in the current Graphoratory repository.

Before changing code:

1. read `AGENTS.md` completely;
2. read `docs/PROJECT.md` completely;
3. inspect the legacy Mutation Forge Lab and HEG repositories available locally as read-only references;
4. specifically inspect their current `pyproject.toml`, graph-generation implementation, graph validation, configuration, and relevant tests.

Treat `AGENTS.md` as binding project instructions and `docs/PROJECT.md` as architectural context.

Do not redesign the project beyond the current milestone.

## Current milestone

Implement only the first operational foundation:

1. clean Python 3.12 project using `uv`;
2. package name `graphoratory`;
3. CLI executable `graphlab`;
4. root default configuration with `key=value` overrides;
5. centralized typed hash identifiers:
   - `ws-xxxxxxxx`
   - `cp-xxxxxxxx`
   - `gr-xxxxxxxx`
   - `ln-xxxxxxxx`
6. SQLite initialization with migrations;
7. `workspace init`;
8. persistent graph corpus generation;
9. line creation with a fixed graph subset;
10. `workspace status`;
11. `line status`;
12. `workspace reindex`.

Use compressed JSON Lines for the initial corpus representation unless inspection reveals a concrete reason that makes this inappropriate.

Do not create one file per graph.

Do not implement:

- policy generation;
- App Server;
- mutation;
- policy evaluation;
- repair;
- baselines;
- branch search;
- `line next`;
- `line run`;
- concurrency;
- counterexample search;
- dashboard.

Use the scientifically validated graph-generation behavior from HEG / legacy Mutation Forge Lab where relevant rather than inventing a different graph family.

## Required engineering behavior

- filesystem artifacts are authoritative;
- SQLite is rebuildable;
- no mutable authoritative state files;
- completed artifacts are published atomically;
- CLI handlers remain thin;
- application operations are reusable directly by future automation;
- all human-facing typed IDs are lowercase;
- full hashes are used internally;
- directory names contain short typed IDs only;
- no implementation-generation labels such as `v3`, `v4`, `preview`, etc.

## Tests

Add focused tests for:

- config parsing and overrides;
- identifier creation and resolution;
- workspace creation;
- migrations;
- graph structural validity;
- graph hashing and deterministic generation;
- corpus persistence;
- line graph-subset persistence;
- workspace status;
- line status;
- deletion and complete reconstruction of SQLite from artifacts.

Use small graph counts in tests.

Run and report:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy src
```

Then perform a real CLI smoke run using a small corpus and show:

- generated workspace ID;
- corpus ID;
- line ID;
- graph count;
- line subset count;
- corpus compressed size;
- SQLite size;
- total workspace size;
- corpus-generation runtime;
- peak RSS if it can be collected without introducing heavy profiling infrastructure.

Stop after this milestone.

Do not begin the next milestone.