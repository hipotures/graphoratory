# Residual C4 separation at order 32

## Question

During the order-32 Erdős–Gyárfás search, low-total graphs repeatedly appeared with profiles

- `(C4,C8,C16,C32)=(4,0,0,0)`, and
- `(C4,C8,C16,C32)=(3,1,0,0)`.

This suggested a structural question independent of the search score itself:

> When the only remaining power-of-two cycles are a small number of 4-cycles, are those 4-cycles spatially separated inside the graph?

For two 4-cycles `A` and `B`, define their graph distance by

\[
d(A,B)=\min_{u\in V(A),v\in V(B)} d_G(u,v),
\]

where `d_G` is ordinary shortest-path distance in the full graph. Thus `d=0` means the cycles share a vertex, `d=1` means they are vertex-disjoint but adjacent by an edge, and larger values represent increasing separation.

This note records an exploratory computational observation, **not a theorem**.

## Initial sample

The order-32 long-run hit log contained:

- 326 labeled hits with profile `(4,0,0,0)`;
- 5,396 labeled hits with profile `(3,1,0,0)`.

For `(4,0,0,0)`, the four 4-cycles yield six unordered cycle pairs per graph. Across the 326 labeled graphs:

- 325/326 graphs (99.7%) had four vertex-disjoint 4-cycles;
- 325/326 had `|union V(C4)|=16`;
- median per-graph minimum cycle distance was `3`;
- median per-graph mean cycle distance was `6.667`;
- median per-graph maximum cycle distance was `8`.

For `(3,1,0,0)`:

- 5,396/5,396 graphs had three vertex-disjoint 4-cycles;
- all had `|union V(C4)|=12`;
- median per-graph minimum cycle distance was `4`;
- median per-graph mean cycle distance was `6`;
- median per-graph maximum cycle distance was `7`.

Because the search repeatedly revisits the same unlabeled structures under different labelings, these raw counts are not suitable for structural frequency claims.

## Exact isomorphism deduplication

Exact graph-isomorphism deduplication was then performed with NetworkX VF2. Weisfeiler–Lehman hashing and simple invariants were used only to pre-bucket candidates; VF2 made the final isomorphism decision.

The labeled hit counts collapsed to:

| profile | labeled hits | non-isomorphic classes | reduction factor |
|---|---:|---:|---:|
| `(4,0,0,0)` | 326 | **113** | 2.885x |
| `(3,1,0,0)` | 5,396 | **148** | 36.459x |

This is important for interpreting the search dynamics. The enormous raw frequency advantage of `(3,1,0,0)` is largely due to repeated visits to the same isomorphism classes; after deduplication the class counts are 148 versus 113.

## Geometry after isomorphism deduplication

For the 113 non-isomorphic `(4,0,0,0)` classes:

- 112/113 (99.1%) have all four 4-cycles vertex-disjoint;
- 112/113 have `|union V(C4)|=16`;
- mean distance over all 678 cycle pairs is `6.5295`;
- median per-graph `d_min` is `3`;
- median per-graph `d_mean` is `6.667`;
- median per-graph `d_max` is `9`.

For the 148 non-isomorphic `(3,1,0,0)` classes:

- 148/148 have all three 4-cycles vertex-disjoint;
- all have `|union V(C4)|=12`;
- mean distance over all 444 cycle pairs is about `7.15`;
- median per-graph `d_min` is `5`;
- median per-graph `d_mean` is `7`;
- median per-graph `d_max` is `7`.

The persistence of near-complete vertex disjointness after exact isomorphism deduplication shows that the separation signal is not merely an artifact of repeated relabelings of a small number of graphs.

## Fixed-C4 control experiment

The central confounder in the preceding comparison is that `(4,0,0,0)` and `(3,1,0,0)` contain different numbers of 4-cycles. A dedicated control search was therefore run at order 32.

The collector fixed the condition `C4=4` and sampled graphs for which at least one higher forbidden power-of-two component was certified positive. The control was bounded-memory and stratified by search phase. In a 5,000,000-candidate run it observed:

- RANDOM-phase dirty candidates: 308,171 seen, reservoir sample 2,500;
- ELITE-phase dirty candidates: 494,535 seen, reservoir sample 2,500;
- clean exact `C4=4` candidates: 69 RANDOM and 130 ELITE;
- score-budget failures: 0;
- witness-cap failures: 0.

After exact isomorphism deduplication:

- clean target `(4,0,0,0)`: 113 classes;
- RANDOM dirty control: 2,496 classes from 2,500 sampled graphs;
- ELITE dirty control: 2,448 classes from 2,500 sampled graphs.

The almost one-to-one RANDOM labeled-to-isomorphism ratio shows that the control sample is structurally diverse.

### Unmatched control result

At fixed `C4=4`, before matching graph density:

| group | non-isomorphic classes | mean pair distance | median `d_min` | all four C4 vertex-disjoint |
|---|---:|---:|---:|---:|
| clean `(4,0,0,0)` | 113 | **6.5295** | **3** | **99.1%** |
| RANDOM dirty | 2,496 | **2.0538** | **0** | **28.8%** |
| ELITE dirty | 2,448 | **2.9329** | **0** | **43.7%** |

However, the dirty controls are substantially denser. The clean targets have almost exclusively 48 or 49 edges, whereas the dirty controls span roughly 48–61 edges. Since additional edges shorten graph distances and can create cycle overlap, density is an important confounder.

## Matched density and degree-sequence comparison

The analysis was therefore repeated at exact edge count and, where possible, exact degree sequence.

### m=48: cubic graphs

At `n=32`, `m=48` and minimum degree at least three force every vertex to have degree exactly three. Thus both groups are cubic with degree sequence `3^32`.

| | clean | RANDOM dirty |
|---|---:|---:|
| non-isomorphic classes | 53 | 7 |
| mean per-graph pair distance | **6.928** | **4.333** |
| median `d_min` | **3** | **1** |
| all four C4 vertex-disjoint | **100.0%** | **71.4%** |

The dirty sample is too small for this stratum to stand alone, but its direction agrees with the larger matched comparison below.

### m=49 with identical degree sequence

For `m=49`, the main common degree-sequence stratum is

\[
3^{30}4^2.
\]

This controls order, edge count, minimum degree, full degree sequence, and the exact number of 4-cycles.

| | clean | RANDOM dirty |
|---|---:|---:|
| non-isomorphic classes | **58** | **94** |
| mean per-graph pair distance | **6.207** | **3.672** |
| median `d_min` | **3** | **1** |
| all four C4 vertex-disjoint | **98.3%** | **56.4%** |

The mean-distance difference is about `+2.535` edges in the clean group, or roughly 69% relative to the dirty-control mean.

Because this comparison fixes `n`, `m`, the full degree sequence, and `C4=4`, the observed separation difference cannot be explained solely by graph density or degree distribution.

## Current empirical conclusion

The strongest defensible statement from these experiments is:

> Among the sampled non-isomorphic order-32 graphs with exactly four 4-cycles, graphs containing no 8-, 16-, or 32-cycles exhibit substantially stronger spatial separation of their four 4-cycles than matched graphs containing at least one higher power-of-two cycle.

This remains an **association observed in search-derived samples**, not a proof that absence of longer power-of-two cycles forces 4-cycle repulsion.

The effect survives:

1. exact graph-isomorphism deduplication;
2. fixing the number of 4-cycles;
3. matching the number of edges;
4. matching the complete degree sequence in the principal `m=49` stratum.

That makes it a plausible structural hypothesis worth testing on independently generated graph families and, potentially, attempting to explain combinatorially.

## Limitations

- The samples are produced by a mutation search and are not IID draws from any natural distribution over graphs.
- The RANDOM phase reduces score-selection bias but does not make the sample uniform over admissible graphs.
- No causal claim is justified by the current analysis.
- No theorem currently follows from the observed distances.
- The analysis concerns order 32; generalization to other orders has not yet been established.

## Possible theoretical direction

A useful theoretical target suggested by the data is to understand whether close interactions among several 4-cycles force a cycle of length 8, 16, or 32 under minimum-degree-at-least-three constraints.

A result of the schematic form

\[
\text{four C4s sufficiently close} \Longrightarrow C_8\text{ or }C_{16}\text{ or }C_{32}
\]

would explain the observed repulsion and could potentially provide additional pruning or structural reductions for an exact counterexample search. The present computation does not establish such a statement; it only motivates it.
