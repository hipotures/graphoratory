# Erdős–Gyárfás computational status — 2026-08-17

## Scope

This note records the current computational status of the Graphoratory experiments on the Erdős–Gyárfás conjecture.

For an admissible graph `G` (simple, connected, minimum degree at least three), let `C_l(G)` denote the number of simple cycles of length `l`. For order `n`, define

\[
F(n)=\min_G \sum_{2^k\le n} C_{2^k}(G).
\]

A counterexample to the Erdős–Gyárfás conjecture at order `n` is exactly a graph with `F(n)=0`.

For heuristic search results we write `T(n)` for the smallest total found so far. A found graph proves only

\[
F(n)\le T(n).
\]

It does **not** prove equality.

## Internally certified exact values

The only exact values established by the current Graphoratory exhaustive pipeline are:

| order | exact value | minimizing profile | exhaustive search |
|---:|---:|---|---:|
| 10 | **F(10)=4** | `(C4,C8)=(4,0)` | 5,203,110 non-isomorphic admissible graphs |
| 11 | **F(11)=2** | `(C4,C8)=(2,0)` | 577,076,528 non-isomorphic admissible graphs |

These results use exhaustive non-isomorphic generation with `nauty/geng` followed by exact cycle counting.

## Constructive upper bounds found by search

The table below records the best currently known Graphoratory constructions. Profiles list the forbidden power-of-two cycle counts in increasing length order.

| n | current upper bound | profile | status |
|---:|---:|---|---|
| 10 | 4 | `(4,0)` | exact |
| 11 | 2 | `(2,0)` | exact |
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

Orders 21 and 22 have not yet been covered by the current constructive calibration/search campaign.

## Confirmatory long run at n=34

A 50,000,000-candidate bounded-memory cascade run at order 34 did not improve the incumbent total of three.

Final incumbent:

\[
(C_4,C_8,C_{16},C_{32})=(3,0,0,0),\qquad m=52.
\]

Run summary:

- evaluated candidates: `50,000,000`
- exact full scores: `11,119`
- certified prunes: `49,988,881`
- score-budget failures: `0`
- witness-cap failures: `0`
- elapsed time: `3410.6 s`
- sustained throughput: about `14,660 candidates/s`
- scorer calls: `C4=50,000,000`, `C8=13,968,498`, `C16=363,699`, `C32=11,119`

This is strong negative search evidence against `T(34)<3`, but it is **not** a lower-bound proof. The formal statement remains only

\[
F(34)\le3.
\]

## External SAT lower-bound frontier

A separate 2026 repository by Arjun Balaji reports a SAT-Modulo-Symmetries computation showing that no minimum-degree-at-least-three counterexample exists for every order `17 <= n <= 31`. Its main method combines SAT Modulo Symmetries (SMS) with the Glasgow Subgraph Solver to forbid `C4`, `C8`, and `C16`; an independent CEGAR-SAT implementation cross-checks the smaller orders.

If independently reproduced, this gives

\[
F(n)\ge1\qquad(17\le n\le31),
\]

and therefore moves the possible order of a counterexample to at least 32.

This external result should currently be treated as a fresh computational claim rather than as an internally certified Graphoratory result. Its own verification notes state that an end-to-end machine-checkable certificate remains future work because forbidden-cycle clauses introduced by the subgraph propagator are not justified by the plain min-degree CNF alone.

Reference repository:

- <https://github.com/ArjunBalaji79/erdos-gyarfas-min-degree-3>

The SMS driver is already written generically: its power-of-two length generator includes every `2^k <= n`. Thus at `n=32` the same formulation naturally becomes `C4,C8,C16,C32` avoidance. This makes `n=32` the most natural next lower-bound target, although its practical runtime is currently unknown.

## Current interval picture

Where an external lower bound and an internal constructive upper bound overlap, the useful statement is an interval rather than a guessed exact value. For example, conditional on reproducing the external SAT frontier:

\[
1\le F(23),\ldots,F(29)\le3,
\]

\[
1\le F(30),F(31)\le4.
\]

For `n >= 32` the current Graphoratory work supplies only constructive upper bounds. In particular:

\[
0\le F(32)\le4,
\qquad
0\le F(34)\le3.
\]

Closing the lower side of these intervals is a separate certification problem from improving the constructive upper side.

## Search engineering result

For `n >= 32`, exact proof of absence of `C32` dominates naive scoring. The current cascade scores in the order

`C4 -> C8 -> C16 -> C32`

and conservatively prunes once a proven non-negative prefix is already worse than the incumbent. The bounded-memory implementation stores only bounded reservoirs and rare exact candidates rather than every seen graph hash.

This changed the practical scale of the search from a few thousand degrading evaluations per second to roughly ten to fifteen thousand stable candidate evaluations per second on the current workstation, while preserving the incumbent comparison semantics.

## Scientific interpretation

The current program now has two distinct computational objectives:

1. **constructive side:** lower the best known `T(n)` and ultimately find `T(n)=0`;
2. **certification side:** prove `F(n)>=1` for additional orders and thereby move the counterexample frontier upward.

These objectives are complementary. A certified non-existence result at a new order is scientifically useful even if the global conjecture remains open, while the constructive search continues to probe the opposite direction.
