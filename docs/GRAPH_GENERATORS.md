# Graph Generators

This document describes the graph generators implemented in Graphoratory. It records the
actual algorithms and configuration after the multiple-generator milestone.

## Why the generation distribution matters

Policy search can overfit to the graph distribution used during development. Regular,
near-cubic, independent-edge, and heterogeneous-degree constructions explore different
parts of the admissible graph space. The `mixed` dispatcher reduces dependence on one
starting distribution, but it does not make the corpus uniform over all admissible graphs.

Later comparisons between policies should use fresh common evaluation graphs. Reusing each
policy's training corpus would confound policy quality with the generator distribution on
which that policy was developed.

## Common corpus contract

Every candidate admitted to a corpus is:

- finite;
- undirected;
- simple;
- connected;
- of minimum degree at least three;
- within the inclusive configured order range.

`Graph.validate_scientific_invariants` is the shared final validator. Generator-specific
code constructs a candidate; it does not bypass this validation.

For attempt number `a`, Graphoratory derives a 64-bit local seed from the first eight bytes
of:

```text
SHA-256("<root-seed>:<a>")
```

A fresh `random.Random` created from that value samples the candidate order uniformly from
`[min_order, max_order]`, selects a component when `mixed` is active, and drives the
concrete generator. Rejected candidates do not change the requested distinct count. This
makes the ordered corpus hashes deterministic for the same effective configuration, root
seed, Python version, NetworkX version, and supported backend behavior.

The generation attempt limit is:

```text
max(1000, workspace_graph_count * 100)
```

Graph hashes are SHA-256 hashes of canonical JSON containing the order and normalized edge
list. A duplicate hash does not count toward `workspace_graph_count`. Exhausting the attempt
limit raises an error reporting accepted, rejected, and duplicate counts.

## `cycle_matching_stub_pairing`

### Algorithm

This is the preserved original Graphoratory generator, structurally identical to the
corresponding HEG seed construction for the same RNG state.

For even order `n`, it starts with the labeled Hamiltonian cycle
`0-1-...-(n-1)-0`, randomly pairs vertices into a perfect matching while forbidding cycle
edges, and returns the union. The result is connected and cubic. Matching construction has
a bounded retry budget of 200.

For odd `n`, it targets the degree sequence `(4, 3, ..., 3)`. It shuffles vertex stubs and
greedily pairs them while rejecting loops, parallel edges, and edges between degree-four
vertices. It additionally requires connectivity and a degree-three neighbor for every
vertex. Stub construction has a bounded retry budget of 2,000.

### Configuration

The generator has no private numeric parameters:

```toml
[graphs]
generator = "cycle_matching_stub_pairing"
```

It uses the shared `min_order`, `max_order`, and `seed`.

### Feasibility and rejection

Even orders must be at least four; odd orders must be at least five. A candidate attempt is
rejected if its bounded internal matching or stub-pairing construction cannot finish.

### Structural bias

Even graphs are exactly cubic and contain the fixed labeled Hamiltonian cycle before
hashing. Odd graphs have exactly one degree-four vertex and all other vertices degree
three. This is a narrow, sparse distribution rather than an unrestricted sample of
minimum-degree-three graphs.

### Policy-search advantages and limitations

The generator preserves the validated HEG starting family and supplies sparse graphs that
are cheap to process. Its strong degree and construction bias can encourage policies that
specialize to cubic or almost-cubic inputs, so it should not be the only development
distribution.

## `random_regular`

### Algorithm

The generator chooses uniformly from degrees in the configured range that satisfy:

```text
d >= 3
d < n
n * d is even
```

It then calls NetworkX `random_regular_graph(d, n, seed=...)`. The shared validator rejects
disconnected candidates.

### Configuration

```toml
[graphs]
generator = "random_regular"

[graphs.random_regular]
degree_min = 3
degree_max = 6
```

### Feasibility and rejection

If a sampled order has no feasible configured degree, that order/degree attempt is rejected
and generation continues deterministically. NetworkX construction failure or common
validation failure also rejects the candidate. Configuration loading fails when the entire
configured order interval contains no feasible `(n, d)` pair.

### Structural bias

Every accepted graph is regular, but the selected degree can vary between graphs. With the
default range this family includes cubic through degree-six graphs when parity permits.

### Policy-search advantages and limitations

It separates degree from order cleanly and provides regular graphs beyond the preserved
cubic construction. It contains no within-graph degree heterogeneity, so policies developed
only on this family may rely too heavily on uniform local degree.

## `erdos_renyi_rejection`

### Algorithm

For sampled order `n`, the generator samples an expected degree uniformly from the feasible
part of the configured interval, computes:

```text
p = expected_degree / (n - 1)
```

and draws an unmodified NetworkX `fast_gnp_random_graph(n, p)`. It does not repair the
result. The common validator either accepts the original `G(n,p)` candidate or rejects it.

### Configuration

```toml
[graphs]
generator = "erdos_renyi_rejection"

[graphs.erdos_renyi_rejection]
expected_degree_min = 6.0
expected_degree_max = 10.0
```

### Feasibility and rejection

Expected degrees must form a positive, finite, increasing interval. For a particular order,
the upper value is capped at `n - 1`; if the lower value is then infeasible, the candidate
attempt is rejected. Disconnected graphs and graphs with any degree below three are rejected
without repair.

The default expected-degree interval is above the acceptance threshold while remaining
moderate for the current order interval 22 through 63. It avoids the severe density drift
that a single fixed `p` would cause across that interval.

### Structural bias

Edges are independently sampled before conditioning. The accepted corpus is therefore
`G(n,p)` conditioned on connectivity and minimum degree at least three, not an unconditioned
Erdős–Rényi sample and not a uniform sample over admissible graphs.

### Policy-search advantages and limitations

This generator provides broad degree and density variation and lacks the regular generator's
degree symmetry. Rejection conditioning suppresses the sparse tail and can become expensive
for low expected degrees or large orders.

## `degree_sequence_rejection`

### Algorithm

For each vertex, the generator samples a target degree uniformly from the configured range
clipped at `n - 1`. It rejects constant sequences, odd degree sums, and non-graphical
sequences. NetworkX checks graphicality with Erdős–Gallai, constructs a simple
Havel–Hakimi realization, and requires that realization to be connected. It then performs
one requested double-edge swap per edge, with a bounded swap-attempt budget, to reduce the
deterministic Havel–Hakimi layout bias while preserving every vertex degree.

The final common validator rejects a disconnected or otherwise invalid randomized result.
No configuration-model multigraph is admitted.

### Configuration

```toml
[graphs]
generator = "degree_sequence_rejection"

[graphs.degree_sequence_rejection]
degree_min = 3
degree_max = 10
```

### Feasibility and rejection

The minimum target degree must be at least three, the maximum must not be smaller, and the
configured degree/order ranges must permit at least two distinct target degrees for some
order. A candidate is rejected for an infeasible sampled order, constant or odd-sum
sequence, failed graphicality, disconnected realization, failed bounded swaps, or common
validation failure.

### Structural bias

Target degrees are sampled independently before graphicality and connectivity conditioning.
Havel–Hakimi plus a finite number of edge swaps does not sample uniformly from all
realizations of a degree sequence. Accepted graphs nevertheless have genuine within-graph
degree heterogeneity.

### Policy-search advantages and limitations

The generator exposes policies to varied local degree while keeping the degree range easy to
interpret. Rejection and realization bias mean it is not a neutral sample of either degree
sequences or simple graphs.

## `mixed`

### Algorithm

`mixed` is a dispatcher, not a fifth graph-construction algorithm. For each candidate
attempt, `random.Random.choices` selects one configured concrete generator using the
configured positive weights. The selected generator then receives the already sampled order
and the same local RNG stream.

### Configuration

```toml
[graphs]
generator = "mixed"

[graphs.mixed]
generators = [
    "cycle_matching_stub_pairing",
    "random_regular",
    "erdos_renyi_rejection",
    "degree_sequence_rejection",
]
weights = [1.0, 1.0, 1.0, 1.0]
```

Generator and weight arrays must have the same nonzero length. Names must be distinct,
registered concrete generators; recursive `mixed` selection is forbidden. Every weight must
be finite and positive.

### Feasibility, rejection, and structural bias

Component selection weights apply to candidate attempts. Components with higher rejection
rates can therefore contribute fewer accepted graphs than their raw weight share. The corpus
manifest records accepted counts per component so this effect is visible.

Equal default weights avoid hard-coding a dominant family. The resulting distribution is a
weighted mixture of biased component distributions and is not uniform over admissible
graphs.

### Policy-search advantages and limitations

A mixed corpus exposes a line to several structural regimes without changing CLI commands or
seeds. Its component proportions depend on both weights and rejection rates, and a finite
corpus is not guaranteed to contain every configured component.

## Persisted provenance and SQLite

The immutable graph manifest records:

- the selected generator;
- every generator-specific parameter section;
- root seed and inclusive order bounds;
- uniform candidate-order selection;
- requested and actual distinct graph counts;
- ordered graph hashes;
- attempted candidates;
- rejected invalid candidates;
- duplicate candidates;
- accepted distinct graphs;
- accepted count per concrete generator.

No rejected candidate graph is persisted. No machine-specific absolute path is recorded.

The project SQLite `graph_corpora` table contains one derived row per workspace corpus. It
stores the selected generator, canonical JSON generation configuration, requested and actual
counts, and aggregate attempt statistics. `workspace reindex` recreates every row from the
immutable manifests.

## Cheap distribution smoke comparison

A read-only comparison generated 80 distinct graphs per generator with orders 22 through 31,
root seed 401, and the documented defaults. All common invariants passed.

| Generator | Attempts | Rejected | Mean edges | Degree range | Pooled degree variance |
| --- | ---: | ---: | ---: | --- | ---: |
| `cycle_matching_stub_pairing` | 80 | 0 | 40.375 | 3–4 | 0.021 |
| `random_regular` | 80 | 0 | 66.363 | 3–6 | 1.355 |
| `erdos_renyi_rejection` | 100 | 20 | 109.825 | 3–17 | 6.213 |
| `degree_sequence_rejection` | 207 | 127 | 87.788 | 3–10 | 5.060 |
| `mixed` | 100 | 20 | 68.713 | 3–16 | 6.146 |

The mixed accepted counts were 27 preserved-construction, 26 random-regular, 15
Erdős–Rényi, and 12 degree-sequence graphs. These descriptive results demonstrate materially
different distributions; they do not rank generators for Erdős–Gyárfás research.
