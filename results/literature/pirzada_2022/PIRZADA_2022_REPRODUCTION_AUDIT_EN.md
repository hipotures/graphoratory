# Reproduction Audit: *On 2-power unicyclic cubic graphs* (Pirzada, Shah, Baskoro, 2022)

## 1. Scope

This document presents a reproducibility audit of:

> S. Pirzada, M. A. Shah, E. T. Baskoro,  
> *On 2-power unicyclic cubic graphs*,  
> Electronic Journal of Graph Theory and Applications 10(1), 2022, 337–344.  
> DOI: 10.5614/ejgta.2022.10.1.24

The audit focuses primarily on the construction in Theorem 2.1, especially the graphs shown in Figures 2, 3, and 4 and the first graph of the proposed family, \(G_1\).

The goal is not to review the entire literature surrounding the Erdős–Gyárfás conjecture, but to determine whether the published construction actually has the properties claimed in the paper.

---

## 2. Executive summary

Several structural parts of the construction reproduce correctly, but the central claims about cycles whose lengths are powers of two do not.

For a literal reconstruction of the graph shown in Figure 4, we obtain

\[
|V(G_1)|=94,\qquad |E(G_1)|=141,
\]

and the graph is cubic. It also contains a bridge separating it into two components of 47 vertices each.

However, exact cycle enumeration gives

\[
(C_4,C_8,C_{16},C_{32},C_{64})
=
(0,10,0,17664,0),
\]

so that

\[
T(G_1)=0+10+0+17664+0=17674.
\]

This contradicts the caption of Figure 4 and the argument in Theorem 2.1, according to which \(G_1\) contains no cycles of lengths \(4,8,16\), with 32 intended to be the relevant power-of-two cycle length.

The first contradiction already occurs in Figure 3. The graph \(K=G-uv\), which the paper explicitly states has no \(C_4\) or \(C_8\), in fact contains exactly one cycle of length 8.

This conclusion was independently reproduced from the figures and again from the textual construction of Theorem 2.1.

---

# 3. What reproduces correctly

## 3.1. The base graph in Figure 2 is cubic

Figure 2 shows a graph \(G\) on 8 vertices.

A direct reconstruction gives:

- \(n=8\),
- \(m=12\),
- every vertex has degree 3.

Thus the graph is indeed cubic.

The exact cycle counts in the reconstructed graph are:

| cycle length | number of cycles |
|---:|---:|
| 3 | 2 |
| 4 | 2 |
| 5 | 4 |
| 6 | 7 |
| 7 | 8 |
| 8 | 3 |

In particular, this agrees with the paper's statement that the graph contains cycles of lengths 4, 5, 6, 7, and 8.

---

## 3.2. Removing \(uv\) produces two degree-2 vertices

The authors define

\[
K=G-uv.
\]

After deleting this edge:

- the graph still has 8 vertices,
- it has 11 edges,
- exactly two vertices, \(u\) and \(v\), have degree 2,
- the remaining six vertices have degree 3.

This part of the description of Figure 3 is correct.

---

## 3.3. The distance \(d_K(u,v)=3\) is correct

The paper states

\[
d_K(u,v)=3<4.
\]

The literal reconstruction of Figure 3 gives exactly

\[
d_K(u,v)=3.
\]

---

## 3.4. The order of \(X_1\) is 47

The construction of \(X_1\) uses:

- five copies of \(K\): \(H_1,H_2,J_1,J_2,H\),
- one copy of \(K_3\),
- one four-vertex gadget described as \(K_{1,3}+x\).

Therefore

\[
|X_1|=5\cdot 8+3+4=47.
\]

This agrees with the construction.

---

## 3.5. The order of \(G_1\) is 94

The authors take two copies \(X_1\) and \(X_1'\), then connect them by the edge \(z_1z_1'\).

Hence

\[
|G_1|=47+47=94.
\]

The caption of Figure 4 and Table 1 both give 94, which is consistent with the construction.

---

## 3.6. \(G_1\) is cubic

A consistent reconstruction of the construction in Theorem 2.1 gives

- \(n=94\),
- \(m=141\),
- every vertex has degree 3.

Since

\[
2m=282=3\cdot94,
\]

this is indeed a cubic graph.

An independent reconstruction from the textual edge list of Theorem 2.1 also reproduces the same degree sequence.

---

## 3.7. The edge \(z_1z_1'\) is a bridge

Both Figure 4 and the text agree that the only connection between \(X_1\) and \(X_1'\) is

\[
z_1z_1'.
\]

Deleting this edge separates the graph into two components of 47 vertices each.

Therefore no cycle can use vertices from both halves.

For cycle lengths that fit inside one half,

\[
C_k(G_1)=2C_k(X_1).
\]

It also follows immediately that \(C_{64}(G_1)=0\), because every cycle must lie entirely inside a 47-vertex component.

That part of the construction is correct.

---

## 3.8. No \(C_4\) or \(C_{16}\) occurs in the reconstructed \(G_1\)

Two independent exact counting implementations agree that

\[
C_4(G_1)=0,
\qquad
C_{16}(G_1)=0.
\]

These parts of the Figure 4 claim are therefore reproduced.

---

# 4. What does not reproduce

## 4.1. The central error already appears in Figure 3

Immediately below Figure 3, the authors state that \(K\)

> “has no cycles of lengths \(2^2,2^3\)”

that is, no cycles of lengths 4 or 8.

For a literal reconstruction of Figure 3, however,

\[
C_4(K)=0,
\qquad
\boxed{C_8(K)=1}.
\]

An explicit 8-cycle can be read directly from the published figure.

Using positional labels from an independent image reconstruction:

\[
T-BL-R2-R1-C-L1-L2-BR-T.
\]

This is a simple cycle through eight distinct vertices.

Therefore the paper's claim that \(K\) has no \(C_8\) is false for the graph actually shown in Figure 3.

### Consequence

This is not a minor numerical discrepancy.

The graph \(X_1\) contains five copies of \(K\). Each copy preserves its internal \(C_8\), regardless of the additional edges connecting the modules.

Hence

\[
C_8(X_1)\ge5.
\]

Since \(G_1\) contains two copies \(X_1\) and \(X_1'\),

\[
C_8(G_1)\ge10.
\]

Exact enumeration gives precisely

\[
\boxed{C_8(G_1)=10}.
\]

Thus the claim that the graph in Figure 4 contains no cycle of length 8 cannot be correct.

---

## 4.2. Figure 4 necessarily inherits ten \(C_8\)'s

The upper half of Figure 4 contains the five copies

\[
H_1,H_2,J_1,J_2,H.
\]

The lower half contains isomorphic copies.

Since each \(K\) contains one internal \(C_8\),

- \(X_1\) contains at least 5 such cycles,
- \(X_1'\) contains at least 5 such cycles.

The bridge \(z_1z_1'\) cannot remove any of them.

Therefore the modular structure alone already proves

\[
\boxed{C_8(G_1)\ge10}.
\]

No large-scale cycle search is required for this contradiction.

---

## 4.3. \(G_1\) contains 17,664 cycles of length 32

Exact enumeration gives

\[
\boxed{C_{32}(G_1)=17664}.
\]

Because the bridge separates the graph into two identical halves,

\[
C_{32}(X_1)=8832,
\]

and therefore

\[
C_{32}(G_1)=2\cdot8832=17664.
\]

This matters for the paper's use of the term “2-power unicyclic”.

---

## 4.4. Independent counting implementations agree exactly

The result was checked using two independent mechanisms.

### Independent exhaustive DFS enumerator

For the full graph \(G_1\),

\[
(C_4,C_8,C_{16},C_{32},C_{64})
=
(0,10,0,17664,0).
\]

### `ScoreWorker` cross-check

Because of the bridge, it is sufficient to count cycles in one 47-vertex half:

| length | independent DFS on \(X_1\) | ScoreWorker on \(X_1\) |
|---:|---:|---:|
| 4 | 0 | 0 |
| 8 | 5 | 5 |
| 16 | 0 | 0 |
| 32 | 8832 | 8832 |

For \(C_{32}\), the ScoreWorker run completed the search with:

- `complete=True`,
- more than 11 million explored nodes,
- exact result `8832`.

The two implementations agree exactly on all relevant counts.

A separate independent reconstruction from the published figures and the text of Theorem 2.1 produced the same cycle counts.

---

## 4.5. Vertex-label interpretation around the \(z\)-gadget

Theorem 2.1 lists the added edges

\[
v_1u_2,\;
y_1x_2,\;
v_2w_1,\;
y_2w_2,\;
w_3v,\;
uz_1,\;
u_1z_3,\;
x_1z_4.
\]

The labeling of \(z_2,z_3,z_4\) in Figure 4 is visually difficult to interpret unambiguously.

An earlier graphical reading suggested a possible mismatch between the displayed labels and the textual edge list. However, an independent reconstruction from the **textual construction alone** yields

- \(|X_1|=47\),
- \(|E(X_1)|=70\),
- all vertices of \(X_1\) degree 3 except \(z_1\), which has degree 2,

exactly as claimed in the paper.

Therefore this audit does **not** treat the \(z_2,z_3,z_4\) labeling as an established mathematical error.

It is also irrelevant to the main cycle contradiction: the ten inherited \(C_8\)'s already occur inside the ten copies of \(K\).

---

## 4.6. Typographical inconsistency: 84 instead of 94

After Figure 4, the text refers to

> “a cubic graph \(G_1\) of order 84”

while:

- the construction gives \(47+47=94\),
- the Figure 4 caption gives 94,
- Table 1 gives \(|G_1|=94\).

Thus 84 is a typographical error.

---

# 5. The meaning of “unique 2-power cycle”

The paper uses two formulations that are not logically equivalent.

## 5.1. Strong interpretation: exactly one cycle

The abstract and definition state that the graph contains

> “only one cycle whose length is a power of 2”

and define:

> “A graph which contains a unique 2-power cycle is called a 2-power unicyclic graph.”

The natural interpretation is

\[
\sum_{j\ge2} C_{2^j}(G)=1.
\]

---

## 5.2. Weaker interpretation: only one power-of-two length occurs

Theorem 2.1 is phrased differently:

- the graph contains a cycle of length \(2^k\),
- it contains no cycle of length \(2^t\) for \(t\ne k\).

That only restricts the set of **cycle lengths** that are powers of two. It does not imply that the cycle of length \(2^k\) is unique.

---

## 5.3. \(G_1\) satisfies neither interpretation

For the reconstructed Figure 4 graph,

\[
C_8=10,
\qquad
C_{32}=17664.
\]

Therefore:

### Strong interpretation

The number of power-of-two cycles is

\[
10+17664=17674,
\]

not one.

### Weaker interpretation

At least two distinct power-of-two lengths occur:

\[
8,\qquad32.
\]

Thus the published \(G_1\) satisfies neither the strong nor the weak interpretation.

---

# 6. Exact reproduction result for \(G_1\)

For the graph corresponding to the published construction:

```text
n = 94
m = 141
cubic = yes
bridge components = [47, 47]

C4  = 0
C8  = 10
C16 = 0
C32 = 17664
C64 = 0

TOTAL = 17674
```

For one half \(X_1\):

```text
C4  = 0
C8  = 5
C16 = 0
C32 = 8832
```

The independent DFS enumerator and ScoreWorker agree on these values.

---

# 7. Errors in the general size formulas

The paper also contains algebraic inconsistencies in the generalized construction.

It gives

\[
|X_i|
=
16\sum_{j=1}^{i}2^j+15.
\]

Since

\[
\sum_{j=1}^{i}2^j=2^{i+1}-2,
\]

the correct closed form is

\[
\boxed{|X_i|=2^{i+5}-17}.
\]

Therefore

\[
\boxed{|X_i|-|X_{i-1}|=2^{i+4}}.
\]

The recurrence printed in the text,

\[
|X_i|=|X_{i-1}|+2^{i+1},
\]

is inconsistent with the paper's own explicit formula.

Since \(G_i\) consists of two copies of \(X_i\),

\[
\boxed{|G_i|=2^{i+6}-34}
\]

and hence

\[
\boxed{|G_i|-|G_{i-1}|=2^{i+5}}.
\]

The text and Table 1 give incompatible alternatives to this recurrence.

For example, the construction correctly yields

\[
|G_1|=94,\qquad
|G_2|=222,\qquad
|G_3|=478.
\]

The main text gives 222 and 478, but Table 1 uses a different recurrence that would imply

\[
|G_2|=158,\qquad
|G_3|=286.
\]

Thus the table contradicts the construction and the numerical values stated in the surrounding text.

---

# 8. Citation and bibliographic inconsistencies

The introduction contains several incorrect reference numbers.

The paper refers to:

- Markström as `[6]`, while the bibliography places Markström at `[8]`,
- Shauger as `[8]`, while the bibliography places Shauger at `[10]`.

These are citation-numbering errors.

There is also a bibliographic inconsistency involving the work cited in the text as “Daniel and Shauger [2]”. The text appears to use the author name correctly, whereas bibliography entry `[2]` prints the first author as “D. Dale” rather than Daniel.

These issues do not affect the graph-theoretic contradiction, but they are additional editorial problems in the paper.

---

# 9. The final argument about the Erdős–Gyárfás conjecture

The paper ultimately moves from its special construction to a claim that a counterexample to the Erdős–Gyárfás conjecture does not exist.

This would require an argument applying to **all** graphs with minimum degree at least 3:

\[
\forall G,\quad
\delta(G)\ge3
\Longrightarrow
G\text{ contains }C_{2^k}
\text{ for some }k.
\]

Constructing an infinite family of cubic graphs that contain a power-of-two cycle is not sufficient to prove such a universal statement.

The concluding argument also contains unsupported logical steps. In particular, the absence of a \(C_{2^{k-1}}\) does not imply that the graph arose by deleting a specific edge \(uv\), nor does it imply a shortest-path equality of the form

\[
d(u,v)=2^{k-1}-1.
\]

Even if one obtained an inequality such as

\[
d(u,v)<\frac n2,
\]

that alone would not contradict a statement about a maximum possible distance or diameter.

The elementary algebra appearing in that paragraph is not the main problem; the problem is that the inequalities are not connected by a valid argument to the claimed universal conclusion.

Furthermore, an argument restricted to cubic graphs would not automatically establish the conjecture for every graph satisfying only

\[
\delta(G)\ge3.
\]

Therefore the claimed resolution of the full Erdős–Gyárfás conjecture does not follow from the argument presented.

---

# 10. Classification of the audited claims

| Item | Audit status |
|---|---|
| Figure 2: 8-vertex graph is cubic | **OK** |
| Figure 2 contains cycles of lengths 4,5,6,7,8 | **OK** |
| \(K=G-uv\) has two degree-2 vertices | **OK** |
| \(d_K(u,v)=3\) | **OK** |
| \(K\) has no \(C_4\) | **OK** |
| \(K\) has no \(C_8\) | **FALSE — \(C_8(K)=1\)** |
| \(|X_1|=47\) | **OK** |
| \(|G_1|=94\) | **OK** |
| \(G_1\) is cubic | **OK** |
| \(z_1z_1'\) is a bridge | **OK** |
| \(G_1\) has no \(C_4\) | **OK** |
| \(G_1\) has no \(C_8\) | **FALSE — \(C_8(G_1)=10\)** |
| \(G_1\) has no \(C_{16}\) | **OK** |
| \(G_1\) contains \(C_{32}\) | **YES — 17,664 such cycles** |
| \(G_1\) has no \(C_{64}\) | **OK** |
| \(G_1\) has exactly one power-of-two cycle | **FALSE** |
| “order 84” after Figure 4 | **TYPO — should be 94** |
| generalized recurrence for \(|X_i|\) | **INCORRECT** |
| generalized recurrence / Table 1 for \(|G_i|\) | **INCORRECT / INTERNALLY INCONSISTENT** |
| construction of \(G_1\) establishes Theorem 2.1 as stated | **NO** |
| paper establishes the full Erdős–Gyárfás conjecture | **NO — does not follow from the presented argument** |

---

# 11. Computational reproduction

Reproduction script:

```text
scripts/heg_pirzada_g1_verify.py
```

Basic verification:

```bash
uv run python scripts/heg_pirzada_g1_verify.py
```

Representative output:

```text
Pirzada-Shah-Baskoro G1 literal verification
Fig.2 G: n=8 m=12 cubic=yes cycles C4..C8={4: 2, 5: 4, 6: 7, 7: 8, 8: 3}
Fig.3 K=G-uv: m=11 deg(u)=deg(v)=2 d_K(u,v)=3 C4=0 C8=1
G1: n=94 m=141 cubic=yes bridge-components=[47, 47]
EXACT C4=0 C8=10 C16=0 C32=17664 C64=0 TOTAL=17674
PAPER CLAIM CHECK: MISMATCH
```

Independent ScoreWorker cross-check:

```text
ScoreWorker half cross-check: MATCH
C4:  half=0
C8:  half=5
C16: half=0
C32: half=8832 complete=True
```

Because the two halves are joined by a bridge,

\[
C_k(G_1)=2C_k(X_1)
\]

for \(k=4,8,16,32\).

---

# 12. Independent cross-validation

The main findings were reproduced independently through three routes:

1. **Figure 2 reconstructed directly from the rasterized PDF**  
   The resulting graph has the same degree sequence and the same exact cycle counts.

2. **Figure 3 reconstructed directly from the PDF and compared with Figure 2**  
   The deleted edge is identified unambiguously and the resulting graph has
   \[
   C_4(K)=0,\qquad C_8(K)=1.
   \]

3. **\(G_1\) reconstructed independently from the textual construction in Theorem 2.1**  
   This gives
   \[
   |X_1|=47,\quad |G_1|=94,
   \]
   the correct cubic degree sequence, the bridge structure, and exactly
   \[
   C_4=0,\quad C_8=10,\quad C_{16}=0,\quad C_{32}=17664.
   \]

The agreement between image-based reconstruction, text-based reconstruction, and two independent cycle-counting implementations makes a transcription or implementation error an implausible explanation for the discrepancy.

---

# 13. Conclusion

The basic structural parts of the construction — the cubic base graph, deletion of \(uv\), the size of \(X_1\), the size and cubicity of \(G_1\), and the bridge between its two halves — can be reproduced.

The central cycle property cannot.

The graph \(K\) in Figure 3 already contains a cycle of length 8, despite the explicit statement that it does not. Since \(G_1\) contains ten copies of \(K\), it inherits ten such \(C_8\)'s. Exact enumeration confirms exactly 10.

In addition, \(G_1\) contains 17,664 cycles of length 32, so it is not “2-power unicyclic” in the sense of having exactly one cycle whose length is a power of two.

The generalized order formulas and Table 1 contain further algebraic inconsistencies, and the final argument does not establish the Erdős–Gyárfás conjecture for all graphs of minimum degree at least 3.

**Reproduction conclusion:** the published construction \(G_1\) does not have the cycle properties claimed for it, and therefore cannot serve as a verified example of a graph with a unique power-of-two cycle or as a valid basis for the claimed resolution of the Erdős–Gyárfás conjecture.

---

## Materials used

1. Original PDF of Pirzada–Shah–Baskoro, EJGTA 10(1), 2022.
2. Figures 2, 3, and 4 and the text of Theorem 2.1.
3. Literal graph reconstruction in `heg_pirzada_g1_verify.py`.
4. Independent exhaustive simple-cycle enumerator.
5. `graphoratory` / `ScoreWorker` exact-cycle cross-check.
6. Independent image-based and text-based reconstruction used as external cross-validation.
