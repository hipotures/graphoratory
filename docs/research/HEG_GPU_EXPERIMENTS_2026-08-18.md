# HEG GPU acceleration experiments — 2026-08-18

## Scope

This note records the GPU acceleration investigation performed for the exact Erdős–Gyárfás search in Graphoratory. Its purpose is not only to preserve the positive result, but also to record the unsuccessful approaches and the reasons they were rejected, so that future work does not need to reconstruct the same experimental path.

The target exact search is the SAT-Modulo-Symmetries (SMS/SMSG) formulation for simple graphs of minimum degree at least three that avoid all power-of-two cycle lengths relevant at the current order. At `n=33`, the forbidden lengths are

`C4, C8, C16, C32`.

The final result of this investigation is narrow but useful:

> A batched CUDA filter that rejects partial SMS states whose current TRUE-edge graph already contains a `C4` is a promising integration candidate. On a 200-state real `n=33`, cutoff-320 pilot, it reduced median end-to-end wall time from `0.414 s` to `0.293 s`, a `1.416x` speedup, with zero correctness mismatches across three repeats.

This is **not** a claim that the full exact `n=33` search is `1.416x` faster. The measured result is a pilot on one real frontier and one batched preprocessing architecture. Full multi-worker integration remains to be implemented and measured.

## Exact-search context and toolchain

The experiments used the Graphoratory `n=33` general exact workspace:

```text
results/exact/sms-v2/n33-general
```

The exact SMS toolchain was pinned to:

- SAT Modulo Symmetries: `464f12f1fd36b496e7ba9dcbb622b079de02dce4`
- Glasgow Subgraph Solver: `abd331a7ef57c83961323f0e24f95ace04d6e9bf`
- CaDiCaL submodule: `b023aaf059babf867a7fdfc5fb342d52ffbccb25`

The GPU experiments were run on the local workstation with an RTX 4070 Ti and a Ryzen 9 7950X3D. The machine is also used interactively, so occasional timing cliffs observed in early microbenchmarks were treated as desktop-noise artifacts rather than as stable performance features. Later comparisons used repeated measurements and medians.

The production exact search is a separate multi-worker workload. The final A/B pilot described below used one SMSG process in cube-file mode. Therefore the result should not be extrapolated directly to the existing 12-worker exact runner.

## Representation and correctness conventions

For undirected graphs on `n` vertices, SMS allocates graph-edge variables first, in lexicographic upper-triangle order:

```text
(0,1), (0,2), ..., (0,n-1), (1,2), ..., (n-2,n-1)
```

Thus edge variables `1..n(n-1)/2` map directly to this ordering. At `n=33` there are `528` edge variables.

GPU state representations used two `uint64` row sets per vertex:

- `TRUE`: currently present edges;
- `UNKNOWN`: currently unassigned edges.

FALSE edges are the remaining off-diagonal pairs.

For a simple undirected graph, the TRUE subgraph contains a `C4` iff some pair of distinct vertices has at least two common TRUE neighbours. This gives an exact bitset test:

```text
exists u < v such that popcount(N_TRUE(u) & N_TRUE(v)) >= 2
```

A partial state that already contains a TRUE `C4` is unsatisfiable under the HEG forbidden-cycle condition. Rejecting such a cube before SMSG is therefore logically sound.

### Why SMS cutoff snapshots can already contain a TRUE C4

This initially looked suspicious, but it is expected under the SMS execution model. The forbidden-subgraph checker for partial graphs is periodic rather than necessarily invoked after every assignment. A simple-assignment cutoff can therefore emit a partial state after a TRUE `C4` has appeared but before the next forbidden-subgraph check removes that branch.

Such states are not parser errors. They are exactly the kind of redundant work a front-end GPU conflict filter can remove.

## Experimental path and decisions

### 1. Dense adjacency matrices / PyTorch-style GPU operations — NO-GO

The first direction was to treat adjacency matrices densely and use GPU matrix operations.

This was rejected for two reasons:

1. At `n <= 64`, the graph fits naturally into one `uint64` adjacency row per vertex. Dense matrix representation wastes work and transfer bandwidth.
2. Matrix powers do not directly count exact simple cycles of long lengths such as `C32`; repeated-vertex closed walks contaminate the result.

For exact `C4` work, a dense GPU implementation was slower end-to-end than the CPU baseline once representation and transfers were included.

**Decision:** do not revisit dense matrix GPU representations for this exact-search path unless the problem representation changes substantially.

### 2. ParaFROST as a drop-in GPU SAT backend — NO-GO

ParaFROST was evaluated as a possible GPU-accelerated replacement for the SAT backend.

It demonstrated GPU inprocessing, but this did not match the architecture of SMSG:

- the core CDCL loop remained largely CPU-bound;
- the GPU was mostly active during preprocessing/inprocessing rather than throughout the search;
- most importantly, the current SMS/Glasgow exact formulation relies on external propagator callbacks and dynamic symmetry logic that are not available as a drop-in equivalent in ParaFROST.

Replacing CaDiCaL in this way would lose essential SMS/Glasgow semantics rather than simply accelerate them.

**Decision:** ParaFROST is not a viable direct backend replacement for the current exact HEG SMS architecture.

### 3. Custom `uint64` CUDA C4 kernels — compute GO

A custom CUDA bitset implementation was then tested with one warp per graph.

Representative resident-kernel measurements at batch size `1,000,000` were approximately:

| operation | GPU | CPU-16 baseline | approximate ratio |
|---|---:|---:|---:|
| exact C4 counting | `~939 M graphs/s` | `~86–90 M/s` | `~10–11x` |
| exact C4 partial propagation | `~528 M graphs/s` | `~40–42 M/s` | `~12–13x` |

These measurements established that the **computation itself** maps very well to CUDA.

However, a one-operation H2D -> kernel -> D2H pipeline was slower than CPU-16 for small/ordinary batches because PCIe and synchronization dominated.

**Decision:** custom bitset CUDA compute is a GO, but only in a batched or persistent architecture. This did not establish an SMSG speedup by itself.

### 4. Persistent VRAM pipeline — GO as an architecture primitive

A persistent pipeline kept TRUE and UNKNOWN graph rows resident on the GPU and applied repeated graph-state updates without re-uploading the full state each round.

At `n=33`, `B=1,000,000`, `K=100` repeated updates:

- propagation-only resident work was roughly `5.7x` CPU-16;
- amortized including transfers was roughly `5.4x`;
- propagation plus C4 count was roughly `5x` resident and `4.8x` amortized.

At `K=10`, the amortized gain was about `3x`. At `K=1`, transfers dominated and GPU was slower.

**Decision:** persistent/batched GPU state is viable. Fine-grained synchronous offload is not.

### 5. Synchronous delta service with full forced-edge bitset — NO-GO

The next test modeled a solver sending two compact `uint16` edge-state deltas per graph per round, with graph state resident on GPU. The GPU applied the deltas and performed exact C4 propagation.

Returning the complete forced-edge set at `n=33` requires `528` bits, stored as 9 `uint64` words = `72 B/graph`.

Despite cheap input deltas, the full result transfer and synchronization were too expensive. The bitset-return service was approximately break-even or worse around realistic smaller batches.

**Decision:** full forced-edge bitset D2H is not suitable for a synchronous per-step oracle.

### 6. Count-only and compact-K result formats — technically promising, but not the final path

A count-only result reduced D2H to `8 B/graph`. On the local RTX 4070 Ti the crossover versus CPU-16 appeared around several hundred states, and large batches became much faster:

| batch | approximate GPU / CPU-16 ratio, count-only |
|---:|---:|
| 256 | `0.99x` |
| 512 | `1.69x` |
| 1,000 | `2.14x` |
| 2,000 | `3.31x` |
| 5,000 | `4.72x` |
| 10,000 | `6.82x` |

Compact exact result formats were then tested:

```text
uint16 forced_count
first K uint16 edge IDs
```

with `K = 1,2,4,8,16`.

For synthetic states, `K=4..16` reduced transfer volume substantially and delivered roughly `5–6x` CPU-16 throughput at large batches. However, overflow behaviour turned out to depend strongly on real SMS depth, making synthetic distributions inadequate for choosing K.

**Decision:** compact output is a valid technical mechanism, but it is not needed for the final conflict-filter design because the final design returns only one conflict flag per state.

## Real SMS state sampling

To stop optimizing against synthetic graph states, the investigation switched to real SMS cutoff snapshots.

The sampler retained:

- the complete original SMS cube;
- TRUE rows;
- UNKNOWN rows;
- actual number of graph-edge assignments;
- requested cutoff;
- a hash of the exact graph state.

Graph-state deduplication used `(TRUE, UNKNOWN)` exactly; it did not attempt graph isomorphism deduplication. The full original cube was preserved because auxiliary/non-edge SAT literals may matter to later SMS solving.

A larger real-state sample contained `59,361` unique `n=33` snapshots. The requested-cutoff distribution was:

| requested cutoff | records |
|---:|---:|
| 100 | 20 |
| 140 | 1,402 |
| 180 | 11,991 |
| 220 | 10,001 |
| 260 | 11,947 |
| 320 | 12,000 |
| 400 | 12,000 |

These are deterministic traversal samples, not IID random samples from the complete search tree.

### Forced-edge distribution on real states

The number of C4-forced FALSE edges increases sharply with depth:

| cutoff | mean forced | median | p90 | p95 | p99 | max | overflow K=8 | overflow K=16 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 180 | 2.17 | 2 | 5 | 7 | 9 | 18 | 1.88% | 0.01% |
| 220 | 4.50 | 4 | 9 | 11 | 16 | 29 | 11.34% | 0.73% |
| 260 | 7.82 | 7 | 16 | 20 | 30 | 62 | 36.99% | 9.37% |
| 320 | 41.12 | 44 | 69 | 77 | 92 | 109 | 86.39% | 77.08% |
| 400 | 64.37 | 66 | 92 | 97 | 108 | 124 | 99.12% | 93.98% |

This showed that a single fixed compact-K protocol is not robust across frontier depth. At deep cutoffs, a direct full bitset would actually move fewer expected bytes than a compact result followed by frequent overflow fallback.

### Synchronous real-state replay crossover

Strict synchronous replay with real snapshots still required large batches before GPU became beneficial. Depending on depth and return format, crossover was typically around `~1,000–2,000` states, with strong gains appearing at `5,000–10,000` states.

**Decision:** the GPU should be placed at a **batched frontier boundary**, not called synchronously by each individual SMS worker.

## Offline C4 preconditioning experiment

The next question was whether GPU-derived C4 implications would help SMSG even without modifying SMS internals.

For each real SMS cube, the GPU computed:

1. whether the TRUE graph already contained a `C4`;
2. otherwise, all UNKNOWN edges that are forced FALSE because setting them TRUE would close a TRUE length-3 path into a `C4`.

The original cube was then augmented with those negative edge literals and solved by the same pinned SMSG.

### Batch generation

Three real frontiers were generated with `10,000` unique states each:

```text
cutoff 260: 10,000
cutoff 320: 10,000
cutoff 400: 10,000
```

Generation times were approximately `6.65 s`, `7.45 s`, and `11.53 s` respectively.

The exact CUDA preprocessor classified the 30,000 states as follows:

| cutoff | TRUE-C4 conflicts | conflict rate | SMS survivors | mean forced edges among survivors |
|---:|---:|---:|---:|---:|
| 260 | 4,763 | 47.63% | 5,237 | 20.49 |
| 320 | 6,239 | 62.39% | 3,761 | 27.95 |
| 400 | 6,106 | 61.06% | 3,894 | 35.95 |
| **total** | **17,108** | **57.03%** | **12,892** | ~27.3 overall |

This was the first strong indication that the most valuable GPU action might be **rejecting already-conflicting states**, rather than propagating forced literals.

## A/B pilot: conflict rejection plus forced-edge augmentation

A pilot was run at `n=33`, requested cutoff `320`.

To reduce traversal-order bias, `200` states were selected evenly across the full `10,000`-state sample rather than taking the first 200.

The two arms were:

```text
A: 200 original cubes -> SMSG

B: GPU preprocessing
   -> TRUE-C4 conflicts terminate before SMSG
   -> surviving cubes receive all C4-forced FALSE edge literals
   -> augmented survivors -> SMSG
```

The same sample was reused for all repeats. Execution order alternated A/B to reduce machine drift.

### Result

The GPU classified `133 / 200 = 66.5%` states as existing TRUE-C4 conflicts, leaving 67 survivors.

Three repeats gave effective end-to-end speedups:

```text
1.371x
1.357x
1.326x
```

Median wall times:

| metric | median |
|---|---:|
| A, original 200 cubes | `0.424 s` |
| B, SMSG on 67 augmented survivors | `0.306 s` |
| amortized GPU preprocessing attribution | `0.0065 s` |
| B effective | `0.313 s` |
| **effective speedup** | **1.357x** |

Correctness mismatches: `0`.

This looked positive overall, but decomposition of the same logs showed that the forced-literal augmentation itself was counterproductive.

### Decomposition

Median SMS CPU time in the original A arm:

```text
states later rejected by GPU: 0.140712 s
surviving states:              0.268879 s
```

The same survivors after forced-edge augmentation required:

```text
0.292685 s
```

Thus survivor-only paired aggregate speedup was:

```text
0.919x
```

In other words, forced-edge augmentation made the survivor subset about `8–9%` slower.

The states eliminated by the GPU accounted for about `34.35%` of the original SMS CPU time. The overall `1.357x` gain came from avoiding those cubes, despite the augmentation penalty on survivors.

**Decision:** forced-edge augmentation is **NO-GO** for the current SMSG path. Do not add dozens of GPU-derived negative edge literals to surviving cubes.

## Final experiment: TRUE-C4 conflict filter only

The final experiment removed forced-edge propagation completely.

A dedicated CUDA kernel returned only one exact conflict flag per state:

```text
A:
200 original cubes
    -> SMSG

B:
200 same partial states
    -> batched CUDA TRUE-C4 filter
       -> conflict: terminate before SMSG
       -> no conflict: send ORIGINAL cube unchanged to SMSG
```

Crucially, this reused the **exact same 200-state sample** as the previous augmentation pilot.

### Dedicated kernel timing

The complete 10,000-state cutoff-320 frontier was stored as two batches of 5,000 states.

Dedicated conflict-only CUDA timings:

| batch | states | conflicts | H2D median | kernel median | D2H median | pipeline median |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 5,000 | 3,258 | `0.104 ms` | `0.0179 ms` | `0.0146 ms` | `0.1409 ms` |
| 1 | 5,000 | 2,981 | `0.0978 ms` | `0.0185 ms` | `0.0123 ms` | `0.1322 ms` |

The kernel itself is extremely cheap; H2D dominates the measured pipeline.

The reported `~0.005 ms` GPU cost for the selected 200-state pilot is an **amortized attribution** from those 5,000-state batches. It must not be interpreted as the cost of launching CUDA separately for only 200 states.

### Final A/B result

The GPU again rejected `133 / 200 = 66.5%` states, leaving 67 unchanged original cubes for SMSG.

Per-repeat effective speedups:

```text
repeat 1: 1.416x
repeat 2: 1.410x
repeat 3: 1.425x
```

Median result:

| metric | median |
|---|---:|
| A, original 200 cubes | `0.413935 s` |
| B, SMSG on 67 unchanged survivors | `0.292654 s` |
| amortized GPU filter cost | `0.00000546 s` |
| B effective | `0.292659 s` |
| **effective speedup** | **1.41594x** |
| correctness mismatches | **0** |
| SAT | 0 |
| timeout | 0 |

This corresponds to about a `29.3%` reduction in wall time for this pilot:

\[
1 - \frac{0.292659}{0.413935} \approx 0.293.
\]

The result is stable across the three alternating-order repeats.

### Identical survivor effect

An important subtlety is that the 67 unchanged survivors were slightly slower when solved in B than when the corresponding cubes were encountered inside A. The per-repeat aggregate ratios `A survivor CPU / B survivor CPU` were approximately:

```text
0.943
0.927
0.941
```

The likely explanation is incremental solver context: in A, SMSG processes the preceding cubes that the GPU filter removes in B, and learned clauses from that work can help later cubes. Therefore removing 66.5% of cube count does **not** translate into a 3x speedup.

This is useful evidence rather than a defect in the experiment: despite losing some useful incremental learning, the conflict-only path still retains a stable net gain of about `1.42x` on this pilot.

## Final decision matrix

| direction | decision | reason |
|---|---|---|
| dense adjacency / matrix GPU | **NO-GO** | poor fit for `n<=64`; transfer/representation cost; long simple cycles not matrix-power exact |
| ParaFROST direct SAT backend | **NO-GO** | does not preserve required SMS/Glasgow callback architecture |
| custom `uint64` CUDA C4 compute | **GO** | very high resident compute throughput |
| persistent VRAM batching | **GO** as primitive | transfers amortize over many states/rounds |
| synchronous full forced-bitset return | **NO-GO** | output bandwidth and synchronization dominate |
| compact K forced-edge return | technically viable | useful only for a forced-edge oracle; depth-dependent overflow complicates protocol |
| forced-edge cube augmentation | **NO-GO end-to-end** | survivors were `~8–9%` slower; aggregate survivor speedup `0.919x` |
| batched TRUE-C4 conflict filter | **GO to integration prototype** | final real-state pilot `1.416x`, stable repeats, zero mismatches |

## Recommended integration architecture

The next implementation should be deliberately narrow:

```text
SMSG frontier / cube generator
        |
        | accumulate a large batch of partial states
        v
CUDA TRUE-C4 conflict filter
        |
        +-- conflict -> mark cube UNSAT / discard before SMS worker
        |
        +-- no conflict -> enqueue ORIGINAL cube unchanged
                              |
                              v
                         normal SMSG worker
```

Recommended properties:

1. **Batch at the frontier boundary.** Do not call CUDA synchronously from each CDCL worker for individual states.
2. **Keep original cubes unchanged.** Do not add GPU-derived forced FALSE literals in the current design.
3. **Preserve all original cube literals.** GPU only needs graph-edge state for filtering, but the survivor sent to SMSG must retain all SAT/auxiliary literals exactly.
4. **Use exact state IDs and deterministic mapping.** Every rejected or surviving state should remain auditable back to the original cube.
5. **Keep an independent correctness path.** During prototype integration, validate a sample of GPU conflict classifications with the CPU bitset criterion.
6. **Use batches in the thousands.** Real replay showed that small synchronous batches do not amortize GPU overhead. The final kernel was measured naturally on 5,000-state batches.

The conflict filter does not need UNKNOWN rows for the actual C4 test; only TRUE adjacency rows are required. A production data path can therefore be smaller than the experimental HEGBAT representation.

## What should not be re-explored without new evidence

The following directions have already been tested sufficiently for the current architecture and should not be restarted merely because they appear intuitively GPU-friendly:

- dense PyTorch/CUDA adjacency matrices;
- matrix-power cycle detection for exact HEG constraints;
- ParaFROST as a drop-in replacement for the current SMS/Glasgow stack;
- per-worker synchronous GPU oracle calls at tiny batch sizes;
- returning complete 528-bit forced-edge masks per state in a fine-grained loop;
- adding all GPU-derived C4-forced FALSE literals to SMS cubes.

These conclusions may be revisited only if a materially different solver architecture, GPU interconnect, or batching model is introduced.

## Limitations of the final result

The final `1.416x` measurement has several important limits.

### It is a pilot, not a whole-search benchmark

The sample contains 200 real `n=33`, cutoff-320 states. It does not measure a complete `n=33` exact run or the current 12-worker queue architecture.

### The frontier sample is not IID

SMS traversal output changes substantially across the sequence. Earlier 5,000-state and later 5,000-state halves at the same requested cutoff showed different conflict rates and forced-edge distributions. The final pilot mitigated this by selecting states evenly over the full 10,000-state frontier and by reusing exactly the same sample in all A/B variants.

### GPU cost is batch-amortized

The final filter cost attributed to 200 selected states was computed from measured 5,000-state batch pipelines. It represents the intended batched architecture, not an isolated 200-state CUDA invocation.

### Incremental learning changes when cubes are removed

SMSG in cube-file mode retains solver state and learned clauses across cubes. Removing conflict cubes can therefore remove learning opportunities as well as work. The survivor timing difference observed in the final test is consistent with this effect.

### Desktop workstation noise exists

The local GPU workstation is used interactively. Early benchmarks showed occasional timing cliffs due to unrelated GPU activity. Final decisions rely on repeated measurements, medians, large batches, and paired A/B structure rather than isolated minima.

## Experimental artifacts and paths

The investigation used the following experimental components. Some are research scripts rather than permanent production interfaces:

```text
benchmark_heg_cuda_bitset.cu
benchmark_heg_cuda_persistent.cu
benchmark_heg_cuda_delta_service.cu
benchmark_heg_cuda_compact_result.cu
heg_sms_snapshot_collect.py
benchmark_heg_cuda_real_snapshots_v2.cu
heg_sms_gpu_batch.py
heg_sms_c4_forced_cuda.cu
heg_sms_c4_ab_runner.py
heg_sms_c4_ab_analyze.py
heg_sms_c4_conflict_cuda.cu
heg_sms_c4_filter_ab_runner.py
```

Important result directories from the final stages were:

```text
results/gpu/c4-ab
results/gpu/c4-ab-pilot-320
results/gpu/c4-filter-final-320
```

The final filter-only summary was written to:

```text
results/gpu/c4-filter-final-320/summary.json
```

The earlier augmentation decomposition was written to:

```text
results/gpu/c4-ab-pilot-320/decomposition.json
```

## Bottom line

The GPU investigation did **not** justify replacing the SAT backend or moving general SMS propagation to CUDA.

It did identify one small operation with a favourable combination of exactness, arithmetic intensity, batching opportunity, and very small output:

> Given a large batch of partial SMS graph states, reject those whose current TRUE-edge graph already contains a `C4`.

For the tested `n=33`, cutoff-320 frontier, this filter removed 66.5% of the selected states before SMSG and produced a stable `1.416x` end-to-end pilot speedup with zero correctness mismatches. Forced-edge augmentation was explicitly tested and rejected because it slowed the survivor subset.

The appropriate next step is therefore a **batched TRUE-C4 rejection-filter integration prototype at the frontier/queue boundary**, followed by a controlled benchmark of the actual multi-worker exact runner. No broader GPU redesign is justified by the current evidence.
