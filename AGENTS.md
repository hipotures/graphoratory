# Graphoratory Agent Instructions

## Project identity

This repository is **Graphoratory**.

- Project name: `graphoratory`
- Python package: `graphoratory`
- CLI command: `graphlab`
- Repository: `hipotures/graphoratory`

Graphoratory is a new implementation. It is not a continuation of the internal architecture of the previous Mutation Forge Lab repository.

The previous Mutation Forge Lab and HEG repositories may be inspected as **read-only reference implementations** for scientifically validated graph logic, evaluator semantics, useful algorithms, dependencies, and small utilities.

Do not modify those repositories.

---

## No product-generation labels

Do not encode implementation generations in source code or runtime concepts.

Do not introduce names such as:

- `v1`, `v2`, `v3`, `v4`, `v5`;
- `preview`;
- `legacy_mode`;
- `next_version`;
- `native_v3`;
- `python_preview`;
- similar generation-specific names.

This applies to:

- modules;
- classes;
- functions;
- configuration keys;
- database tables;
- schemas;
- protocol names;
- artifact directories;
- CLI commands.

Use durable domain terminology instead.

Normal package versions and database migration revision identifiers are exempt from this rule.

---

## Development philosophy

Graphoratory must be built incrementally.

Each meaningful operation should first exist as an independently executable and independently testable application operation and CLI command.

Do not implement large hidden pipelines.

The normal development sequence is:

1. implement one operation;
2. test it;
3. run it manually;
4. inspect its artifacts;
5. inspect SQLite;
6. inspect runtime and disk usage where relevant;
7. only then implement the next operation.

Higher-level automation added later must reuse the same application services as the individual CLI operations.

Do not create a second implementation path for automated execution.

---

## Architecture principles

### Filesystem artifacts are authoritative

Durable filesystem artifacts are the source of truth.

SQLite is a rebuildable index, projection, query layer, and future dashboard data source.

Scientifically or operationally important information must not exist only in SQLite.

The database must be rebuildable from workspace artifacts.

### No authoritative mutable state files

Do not create authoritative files such as:

- `state.json`;
- `current_state.json`;
- `checkpoint.json`;
- `runtime_state.json`.

Current state must be reconstructed from completed immutable artifacts.

SQLite may cache reconstructed state.

### Atomic artifacts

Publish durable artifacts atomically.

A failed command must not leave a partially written artifact that appears complete.

### Minimal persistence

Persist only information required for:

- scientific evidence;
- provenance;
- resumption;
- reconstruction;
- database reindexing;
- resource analysis.

Do not persist large intermediate data merely because it exists in memory.

---

## Identifiers

Use full cryptographic hashes internally.

Use lowercase typed short identifiers for human-facing output.

Current prefixes:

- workspace: `ws-xxxxxxxx`
- line: `ln-xxxxxxxx`
- graph: `gr-xxxxxxxx`
- corpus: `cp-xxxxxxxx`

The short hash is the first 8 hexadecimal characters of the full hash.

Examples:

```text
ws-a1b2c3d4
ln-38aa192f
gr-b782c0e1
cp-91f03a22
```

Rules:

- all identifier prefixes are lowercase;
- hexadecimal hashes are lowercase;
- CLI output uses short typed IDs by default;
- filesystem directory names use typed short IDs;
- SQLite stores full and short hashes;
- relations use full hashes internally;
- CLI accepts short typed IDs and resolves them to full hashes;
- ambiguous prefixes must fail instead of being guessed.

Do not use uppercase identifier prefixes.

---

## Python and tooling

Target Python 3.12.

Use `uv` for environment and dependency management.

Use:

```bash
uv sync
uv run ...
```

Do not establish ordinary `pip` as the project workflow.

Use a `src/` package layout.

Maintain:

- pytest;
- Ruff;
- mypy;
- Rich.

Prefer typed ordinary Python over framework-heavy architecture.

---

## CLI

The CLI executable is:

```text
graphlab
```

Scientific and operational overrides should use `key=value` / dotlist syntax where practical.

Examples:

```bash
graphlab workspace init testowy
graphlab graph generate
graphlab line create workspace=ws-a1b2c3d4
graphlab line status ln-38aa192f
```

Do not silently select a line when a command operates on a line.

A line identifier must be explicit.

Keep CLI handlers thin. Application logic belongs in reusable Python services.

---

## Configuration

The repository root contains the default configuration.

If no explicit configuration is supplied, commands use the root default.

Configuration is editable.

Do not permanently lock a workspace to the current contents of the config.

Historical artifacts record the parameters that produced them.

Do not put derived runtime state into configuration.

Keep the configuration minimal and add fields only when an implemented feature requires them.

---

## Scientific model

Graphoratory supports research related to the Erdős–Gyárfás conjecture.

The scientific search has two distinct phases.

### Policy search

Search for increasingly effective Python graph-mutation policies.

A policy proposes graph rewrites.

The evaluator remains independent from policy code.

The existing scientifically validated policy/evaluator contract from the previous system should be inspected before implementing this phase.

### Counterexample search

After strong policies have been identified and compared independently, selected policies are used for long-running searches for an actual counterexample.

Counterexample search is a later phase and must not be mixed into early policy-search infrastructure.

---

## Workspace graph corpus

A workspace owns a persistent generated graph corpus.

The graph corpus is generated once per corpus-generation operation and saved to disk.

Each graph has a stable content hash.

A line chooses a fixed subset of graph hashes from one corpus.

The same subset is used throughout that line.

Different lines may use different subsets.

This allows direct comparisons within a line while introducing diversity between independent lines.

---

## Lines

A line is one independent policy-search trajectory.

The primary human-facing handle for policy development is the line ID.

A line eventually contains:

- a fixed graph subset;
- generated policies;
- policy evaluations;
- mutation branches;
- immutable completed step artifacts.

A line does not have an authoritative mutable state file.

Its current state is reconstructed from its completed artifacts.

Future convenience commands such as `line next` and `line run` must use this reconstructed state.

---

## Policy search branching

The intended future search model protects a good current head from bad mutations.

Two separate limits will control exploration:

- branch depth;
- branches per head.

A failed branch returns to the current head.

An improving descendant may become the new head.

Counters restart for every new head.

Do not implement this until the corresponding milestone explicitly requests it.

---

## Final policy comparison

Scores from different lines are not automatically treated as final directly comparable rankings because lines may use different graph subsets.

Before counterexample search, selected policies will be evaluated together on the same fresh graph sample or common evaluation stream.

That produces the final comparable evidence used to select policies for long-running search.

---

## AI integration

Codex App Server integration is a later implementation milestone.

When implemented, AI should actively generate and improve policy source based on scientific evidence.

Invalid generated policy code may be repaired using an explicit bounded repair process.

Do not silently substitute fallback policies.

---

## Reference repositories

The existing Mutation Forge Lab repository may be inspected for:

- proven graph-generation behavior;
- HEG integration;
- existing scientific scoring semantics;
- policy contracts;
- Codex App Server integration;
- dependency/tooling choices;
- small reusable utilities.

HEG may be inspected for authoritative mathematical and graph-processing behavior.

Do not copy the previous orchestration architecture merely because it exists.

In particular, do not reintroduce unless explicitly required:

- Directors;
- slots;
- generations as orchestration state;
- campaign managers;
- queues;
- elaborate checkpoint systems;
- multiple competing state stores;
- hidden fallback workflows;
- large artifact dumps.

---

## Scope discipline

Implement only the milestone explicitly requested by the current task.

Do not proactively implement later planned features.

It is acceptable and expected for the repository to remain incomplete between milestones.

At the end of a task:

1. run the relevant tests;
2. run Ruff;
3. run mypy;
4. perform the requested smoke test;
5. report exactly what changed;
6. report anything deliberately left for a later milestone.
7. commit & push
