# Milestone 3 — Baseline and Independent Evaluator

**Status:** IMPLEMENTED; final validation recorded below

## Scientific authority recovered

The relevant legacy authority is the ordinary-Python scientific lane in Mutation Forge
Lab, because later Graphoratory policies will also be ordinary Python. Mutation Forge Lab
still labels its older protocol as the operational default, but its ordinary-Python parity
document explicitly replaces that protocol's point score with conservative interval
evidence and applies the same evaluator to generated policies and built-in baselines.

The authoritative source locations inspected were:

- Mutation Forge Lab
  `src/mutation_forge/native_v3_python/scientific_evaluation.py` and
  `native_v3_python/serial_evaluator.py`: baseline identities, deterministic operator
  invocation, and policy/evaluator separation;
- Mutation Forge Lab `src/mutation_forge/native_v3/scoring.py`: component evidence,
  mixed-radix energy, exact rational utility/AUC/fitness, and strict interval improvement;
- Mutation Forge Lab `src/mutation_forge/native_v3/heg_scoring.py`: locked initial and
  expanded budgets and the mandatory HEG C++ scorer boundary;
- HEG `src/sglab/targets/erdos_gyarfas.py`: forbidden lengths, graph validity, mutation
  operators, witness weights, and score assembly;
- HEG `src/sglab/model.py`, `src/sglab/score_worker.py`, and
  `cpp/sglab_score_worker.cpp`: graph representation, bounded cycle enumeration, and the
  production scorer protocol;
- HEG and Mutation Forge Lab score, worker, and serial-evaluator tests.

Graphoratory pins HEG commit `27cbec9c2307b6ea5f936f858821d11d808b68f3`.
The bundled score-worker source is byte-identical to that commit's
`cpp/sglab_score_worker.cpp`.

## Exact score semantics

For a graph of order \(n\), the evaluator measures capped cycle counts for:

```text
4, 8, 16, ... <= n
```

The cap is 64. Each component is an exact count `[c, c]`, a saturated capped count
`[64, 64]`, or bounded incomplete evidence `[observed, 64]`.

Initial component searches use 50,000 DFS nodes and a 5-second worker timeout. When
candidate and incumbent energy intervals overlap and either is non-point, only non-point
components are retried with 200,000 nodes and a 20-second timeout. Expanded evidence may
not weaken an earlier bound.

For each endpoint, the evaluator forms:

```text
total    = sum(cycle_count[length])
weighted = sum(max(1, 64 // length) * cycle_count[length])
```

It then encodes the lexicographic objective
`(total, weighted, edge_count)` with the exact mixed-radix scale from the legacy evaluator.
Utility is `1 - energy / energy_max`, represented as an exact rational interval.

A proposal is accepted only when:

```text
candidate_energy.upper < incumbent_energy.lower
```

Each graph produces a best-so-far utility trajectory containing the initial state and 32
baseline steps. Its episode score is the exact-rational mean (AUC) of that trajectory.
Line fitness is averaged within each graph order and then equally across represented
orders. The authoritative score is therefore a conservative rational fitness interval,
not a cycle count, shortest-cycle value, or arbitrary scalar energy.

Valid evaluator inputs are nonempty, simple, undirected, connected graphs of order at most
128 and minimum degree at least three.

## Frozen baseline and evaluator boundary

Graphoratory implements one baseline:

```text
heg_uniform_two_switch
```

It is the legacy random baseline using HEG's `uniform_two_edge_switch` operator in
`unrestricted_min_degree_3` mode, seed 4001, horizon 32, and witness cap 64. These values
come from the current sustained ordinary-Python scientific profile. The baseline is frozen
ordinary code and performs no AI or external model activity.

The evaluator depends only on a small policy protocol that proposes a graph. It validates
and scores both incumbents and proposals itself. The baseline neither computes nor
certifies its own score, so a later custom Python policy can use the same evaluator without
changing score semantics.

## Fixed-line graph input

`graphlab baseline evaluate [LINE]`:

1. selects the workspace through the existing precedence;
2. resolves explicit or latest line through the workspace-local SQLite index;
3. reads ordered membership from `line_graphs.position`;
4. checks the one exact line manifest against that projection;
5. reads the one known `graphs/graphs.jsonl.gz` file once;
6. validates the graph manifest and selects exactly the ordered membership.

Ordinary evaluation does not enumerate line or evaluation directories and never scans
other workspaces.

## Artifact and SQLite projection

Every completed evaluation publishes one atomic immutable file:

```text
lines/ln-xxxxxxxx/evaluations/<evaluation-full-hash>.json
```

The artifact stores workspace/line identity, baseline and evaluator provenance, the exact
rational score interval, compact aggregate diagnostics including graph counts by order,
graph count, wall time,
graphs/second, peak RSS, and HEG worker source/binary/compiler identity. It does not repeat
line membership or graph payloads.

The workspace-local `evaluations` table projects the artifact hash, ownership, creation
time, baseline, graph count, and rational score endpoints. Evaluation artifact enumeration
is confined to explicit workspace reindex. Reindex validates the artifact content hash and
reconstructs the table. There is no project-wide database.

Filesystem publication precedes SQLite insertion. An insertion failure retains the
scientific artifact and reports `graphlab workspace reindex`.

## CLI output

The command supports Rich and `--json` output from one semantic payload. It reports the
baseline, line, workspace, graph count, authoritative score, compact diagnostics, runtime,
throughput, peak RSS, artifact hash, and database state.

## Parity validation

Parity fixtures are independent legacy values:

- HEG K5 baseline step 0 with seed 4001 removes edge `(2, 3)` and produces the frozen
  expected graph;
- HEG K4 (`C~`) has three 4-cycles, 24 visited DFS nodes under the 50,000-node budget,
  energy 3123, and utility `61/64`; the complete independent-evaluator fixture asserts
  that exact fitness, not a value regenerated by Graphoratory;
- the mixed-radix hand vector from Mutation Forge Lab for order 8, one length, and cap 4
  has edge range 12..28 and `energy_max = 5524`.

Tests also cover deterministic repeated evaluation, exact SQL membership, cross-line
isolation, no ordinary artifact-directory scan, reindex reconstruction, workspace
isolation, publication-before-index failure behavior, and Rich/JSON agreement.

## Resource measurements

The disposable single-graph CLI smoke produced the exact score `352557/366437`.
Two independent JSON evaluations took 0.5035 s and 0.5096 s respectively, or 1.986 and
1.962 graphs/s. The application-reported peak RSS was approximately 68.3 MB. The Rich
evaluation produced the same score, so the three completed commands published three
immutable artifacts. Reindex reconstructed all three SQLite rows without changing the
second workspace database.

## Deliberate exclusions

This milestone does not implement custom policies, policy generation or mutation, repair,
branching, `line next`, `line run`, scheduling, concurrency, counterexample verification,
common cross-line evaluation, random-access gzip indexing, dashboards, or Codex App Server
integration.
