# Graphoratory

## Purpose

Graphoratory is a minimal, inspectable scientific experimentation system for computational research related to the Erdős–Gyárfás conjecture.

Its long-term objective is to search for a counterexample by:

1. discovering effective Python graph-mutation algorithms;
2. evaluating and improving those algorithms;
3. selecting the strongest policies using independent common evaluation;
4. using selected policies in long-running counterexample searches.

The project deliberately prioritizes simplicity, observability, reproducibility of scientific evaluation, and incremental development over orchestration sophistication.

---

# 1. Why the project is being rebuilt

Previous Mutation Forge Lab implementations accumulated too many interacting mechanisms:

- large orchestration layers;
- multiple states and checkpoints;
- automatic multi-stage workflows;
- extensive intermediate artifacts;
- difficult-to-follow recovery behavior;
- high storage and memory costs.

The new system starts again from the smallest useful scientific kernel.

Every major operation will first be independently executable from the CLI.

Automation is added only after those operations have been individually tested.

---

# 2. Two scientific phases

## 2.1 Policy search

The first computational phase searches for Python code that performs graph mutations effectively.

The policy is repeatedly:

```text
generated
→ evaluated
→ mutated using evaluation evidence
→ evaluated
→ improved further
```

Although there is no conventional machine-learning model training, this process is an optimization loop: evaluation results influence subsequent program generation.

Therefore policies can overfit to the graph samples used during development.

Graph sampling is designed with this in mind.

## 2.2 Counterexample search

Counterexample search is separate.

Only after promising policies have been identified and compared using common independent evaluation are selected policies used for long-running searches.

This phase may eventually run for hours or days.

It is not part of the initial implementation.

---

# 3. Workspace

A workspace is the top-level unit of one research environment.

Conceptually:

```text
workspaces/
└── ws-xxxxxxxx/
    ├── manifest.json
    ├── index.sqlite3
    ├── graphs/
    └── lines/
```

A workspace contains durable scientific artifacts.

Each workspace also has a unique human-readable name in its authoritative manifest.
The canonical `ws-xxxxxxxx` identifier remains the filesystem directory name and
internal identity.

The workspace configuration itself is not frozen permanently.

A user may modify the root configuration and deliberately create new artifacts using different parameters.

Existing artifacts retain their original provenance.

---

# 4. Identifiers

Hash-addressed objects use a full internal hash and a typed human-facing short identifier.

Current types:

```text
ws-xxxxxxxx  workspace
cp-xxxxxxxx  graph corpus
gr-xxxxxxxx  graph
ln-xxxxxxxx  line
```

Rules:

- all prefixes are lowercase;
- short hashes contain 8 hexadecimal characters;
- full hashes are authoritative;
- short identifiers are primarily for humans and CLI input;
- filesystem directory names use short identifiers;
- database relations use full hashes;
- SQLite stores both full and short hashes;
- ambiguous short identifiers must never be guessed.

Later policy and step identifier prefixes will be added when those concepts are implemented.

---

# 5. Configuration

The repository root contains the default configuration.

Commands use it automatically unless another config is explicitly supplied.

The optional top-level `active_workspace` setting selects a workspace by human name or
typed ID for ordinary commands. An explicit `workspace=<name-or-id>` command value takes
precedence. If neither is present, workspace operations fail rather than guessing.

Command-specific configuration overrides use `key=value` syntax where practical.

Example:

```bash
graphlab workspace init testowy graphs.count=1000
```

Configuration contains user decisions, not reconstructed runtime state.

The initial configuration will contain only parameters required by implemented functionality.

---

# 6. Persistent graph corpus

A workspace creates a persistent corpus of generated graphs.

Conceptual example:

```text
1000 graphs
```

The exact value is configurable.

Relevant graph generation behavior should remain scientifically consistent with the validated HEG / previous Mutation Forge Lab implementation.

Graphs must satisfy the required structural constraints, including the appropriate HEG search graph family such as connected simple undirected graphs of minimum degree at least three.

Each graph has a stable content hash.

Initial storage may use one compressed JSON Lines file:

```text
graphs/
└── cp-xxxxxxxx/
    ├── manifest.json
    └── graphs.jsonl.gz
```

The format is intentionally simple.

If profiling later shows graph I/O to be a bottleneck, the storage format can be replaced without changing the higher-level scientific model.

---

# 7. Lines

A line is one independent trajectory through policy space.

When a line is created, it chooses a fixed subset of graph hashes from the workspace corpus.

Example:

```text
workspace corpus: 1000 graphs
line subset:       100 graphs
```

That selection is persisted once.

All policy development inside the line uses exactly that subset.

Different lines may use different random subsets.

This design provides:

- exact within-line comparability;
- diversity between independent trajectories;
- reduced risk that every trajectory adapts to exactly the same small sample.

A line initially contains only its identity and graph membership.

Later milestones add policies, evaluations, mutations, branches, and step artifacts.

---

# 8. Policy development model

A future line will conceptually look like:

```text
initial policy
    ↓
evaluate
    ↓
HEAD
    ↓
mutation branch
    ↓
evaluate
```

A bad mutation must not automatically replace a good head.

Exploration will use two separate concepts:

```text
branch_depth
branches_per_head
```

A branch may contain several mutations.

If a descendant eventually beats the head from which the branch started, it becomes the new head.

If the branch fails, it is abandoned and another branch may start from the same head.

If all branch attempts are exhausted, the line is exhausted.

A new head receives fresh branch counters.

This mechanism is deliberately deferred until the fundamental policy operations exist independently.

---

# 9. Comparing different lines

Different lines may use different development graph subsets.

Therefore their raw development scores are useful evidence but are not automatically the final common ranking.

Before a policy is selected for counterexample search, candidate policies are evaluated together using exactly the same fresh graph sample or generated graph stream.

Conceptually:

```text
candidate policies
       ↓
fresh common graph sample
       ↓
evaluate every policy on the same graphs
       ↓
comparable final scores
       ↓
select policy/policies
```

These temporary final-evaluation graphs do not necessarily need to become part of the permanent workspace corpus.

---

# 10. Durable artifacts

Filesystem artifacts are authoritative.

Examples will eventually include:

- workspace manifest;
- graph corpus;
- corpus manifest;
- line manifest;
- policy source;
- evaluation result;
- immutable completed step manifests.

Artifacts should be small and purpose-specific.

Do not store every intermediate graph or every transient runtime value.

---

# 11. State reconstruction

The system deliberately avoids authoritative mutable state documents.

Current state is derived from completed artifacts.

For example, a future line status operation will infer:

- current policy head;
- completed branches;
- current branch;
- remaining attempts;
- next logical operation;

from line artifacts.

SQLite may store this derived result as a cache.

If the cache is wrong or incomplete, it can be rebuilt.

---

# 12. SQLite

SQLite is present from the beginning.

Its functions are:

- indexing;
- efficient querying;
- future dashboard source;
- convenience cache;
- reconstructed status storage.

It is not the authoritative scientific record.

Required property:

```text
delete SQLite
→ run workspace reindex
→ rebuild database from artifacts
```

Schema changes use migrations.

Database writes should use sensible transactions and avoid high-frequency progress updates.

---

# 13. Crash behavior

Durable command output is written atomically.

Conceptually:

```text
write temporary artifact
→ complete it
→ atomically publish it
→ index it in SQLite
```

If the application fails before publication, the operation can be repeated.

If artifact publication succeeds but SQLite indexing fails, the filesystem artifact remains authoritative and `workspace reindex` can restore the database.

There is no need for an elaborate checkpoint system.

---

# 14. CLI model

The executable is:

```text
graphlab
```

Operations are hierarchical and small.

Initial examples:

```bash
graphlab workspace init testowy
graphlab workspace list
graphlab workspace status
graphlab workspace status testowy
graphlab workspace reindex

graphlab graph generate

graphlab line create
graphlab line status ln-xxxxxxxx
```

Typer owns command groups, subcommands, positional arguments, options, and help.
Trailing `key=value` values are a separate configuration override layer.

Later commands may include:

```text
policy generate
policy validate
policy repair
policy evaluate
policy mutate

line next
line run
```

Those commands do not exist until their corresponding milestone implements them.

---

# 15. Future automation

The initial system is intentionally manual and stepwise.

Once all individual line operations work, `line next` will determine the next logical operation from artifacts and execute exactly one step.

Later:

```text
line run
```

will repeatedly invoke the same underlying transition mechanism.

It must not become a separate implementation.

Eventually several independent lines can run concurrently.

Line-level concurrency is preferred as the first concurrency model because lines naturally spend different periods:

- waiting for AI;
- evaluating policies;
- repairing generated code;
- mutating.

Graph-level evaluation workers should only be added if profiling demonstrates a need.

---

# 16. App Server and AI

Policy generation and mutation will later use Codex App Server.

The AI will be an active algorithm improver.

Mutation prompts will use:

- current policy source;
- evaluation evidence;
- relevant line history;
- policy contract;
- scientific objective.

New-line generation should eventually receive compact summaries of prior lines so that obviously unsuccessful approaches are not endlessly rediscovered.

Invalid AI code may enter a bounded repair process.

No hidden fallback policy should replace failed generated code.

---

# 17. Baselines

Scientifically meaningful existing baselines should be imported or adapted from validated previous code.

They will be implemented as an independent operation after the graph corpus and evaluator foundations exist.

Baseline execution must remain directly runnable rather than hidden inside a larger pipeline.

---

# 18. Resource observability

Runtime behavior must remain visible from early development.

Relevant measurements include:

- wall-clock duration;
- CPU time;
- peak RSS;
- graph throughput;
- artifact sizes;
- SQLite size;
- workspace disk usage.

Profiling infrastructure should not be added unless measurements show a need.

---

# 19. Development roadmap

The intended incremental sequence is approximately:

1. Python project bootstrap;
2. configuration;
3. identifiers;
4. workspace initialization;
5. SQLite migrations;
6. graph corpus generation;
7. graph validation/export;
8. line creation;
9. workspace and line status;
10. SQLite reindex verification;
11. baseline/evaluator foundation;
12. policy generation;
13. policy validation;
14. repair;
15. policy evaluation;
16. policy mutation;
17. mutation branch mechanics;
18. `line next`;
19. `line run`;
20. concurrent lines;
21. final common policy evaluation;
22. counterexample search;
23. independent SQLite-only dashboard.

Each stage receives a separate implementation task and is tested before proceeding.

---

# 20. Reference code

The previous Mutation Forge Lab repository and HEG remain valuable scientific references.

Reuse proven behavior where appropriate.

Do not reproduce legacy orchestration architecture unless there is a new measured requirement for it.

Graphoratory is expected to remain much smaller and more explicit than the systems it replaces.
