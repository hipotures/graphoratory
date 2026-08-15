from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Protocol

from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import forbidden_lengths  # type: ignore[import-untyped]

from graphoratory.errors import BaselineFailure, EvaluationFailure, InvalidGraphError
from graphoratory.graphs import Graph
from graphoratory.science.worker import (
    EXPANDED_NODE_BUDGET,
    INITIAL_NODE_BUDGET,
    ScoreTimeoutWithoutPartial,
    ScoreWorker,
)


class Policy(Protocol):
    name: str
    seed: int
    horizon: int
    witness_cap: int

    def propose(self, graph: Graph, *, step_index: int) -> Graph | None: ...

    def provenance(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class IntegerInterval:
    lower: int
    upper: int

    def __post_init__(self) -> None:
        if self.lower > self.upper:
            raise ValueError("interval lower bound exceeds upper bound")

    @property
    def exact(self) -> bool:
        return self.lower == self.upper

    def payload(self) -> dict[str, int]:
        return {"lower": self.lower, "upper": self.upper}


@dataclass(frozen=True, slots=True)
class RationalInterval:
    lower: Fraction
    upper: Fraction

    def __post_init__(self) -> None:
        if self.lower > self.upper:
            raise ValueError("interval lower bound exceeds upper bound")

    @property
    def exact(self) -> bool:
        return self.lower == self.upper

    def payload(self) -> dict[str, dict[str, int]]:
        return {
            "lower": _fraction_payload(self.lower),
            "upper": _fraction_payload(self.upper),
        }


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    length: int
    observed: int
    lower: int
    upper: int
    status: str
    node_budget: int
    nodes_visited: int

    @property
    def exact(self) -> bool:
        return self.lower == self.upper


@dataclass(frozen=True, slots=True)
class ScoreEvidence:
    graph_hash: str
    order: int
    edge_count: int
    witness_cap: int
    components: tuple[CycleEvidence, ...]

    @property
    def total(self) -> IntegerInterval:
        return IntegerInterval(
            sum(component.lower for component in self.components),
            sum(component.upper for component in self.components),
        )

    @property
    def weighted(self) -> IntegerInterval:
        return IntegerInterval(
            sum(_weight(component.length) * component.lower for component in self.components),
            sum(_weight(component.length) * component.upper for component in self.components),
        )


@dataclass(frozen=True, slots=True)
class EnergyScale:
    order: int
    lengths: tuple[int, ...]
    witness_cap: int
    weighted_max: int
    edge_min: int
    edge_max: int
    energy_max: int

    @classmethod
    def build(cls, order: int, lengths: tuple[int, ...], witness_cap: int) -> EnergyScale:
        total_max = len(lengths) * witness_cap
        weighted_max = sum(_weight(length) * witness_cap for length in lengths)
        edge_min = (3 * order + 1) // 2
        edge_max = order * (order - 1) // 2
        edge_span = edge_max - edge_min
        energy_max = (total_max * (weighted_max + 1) + weighted_max) * (edge_span + 1) + edge_span
        return cls(
            order,
            lengths,
            witness_cap,
            weighted_max,
            edge_min,
            edge_max,
            energy_max,
        )

    def interval(self, evidence: ScoreEvidence) -> IntegerInterval:
        if (
            evidence.order != self.order
            or evidence.witness_cap != self.witness_cap
            or tuple(component.length for component in evidence.components) != self.lengths
        ):
            raise EvaluationFailure("score evidence does not match its energy scale")
        total = evidence.total
        weighted = evidence.weighted
        return IntegerInterval(
            self._encode(total.lower, weighted.lower, evidence.edge_count),
            self._encode(total.upper, weighted.upper, evidence.edge_count),
        )

    def utility(self, energy: IntegerInterval) -> RationalInterval:
        return RationalInterval(
            Fraction(1) - Fraction(energy.upper, self.energy_max),
            Fraction(1) - Fraction(energy.lower, self.energy_max),
        )

    def _encode(self, total: int, weighted: int, edge_count: int) -> int:
        if not self.edge_min <= edge_count <= self.edge_max:
            raise InvalidGraphError("graph edge count is outside the valid energy scale")
        return (
            (total * (self.weighted_max + 1) + weighted) * (self.edge_max - self.edge_min + 1)
            + edge_count
            - self.edge_min
        )


@dataclass(frozen=True, slots=True)
class EvaluationDiagnostics:
    episodes: int
    graphs_by_order: tuple[tuple[int, int], ...]
    proposals: int
    no_proposals: int
    accepted_rewrites: int
    score_attempts: int
    unique_graph_scores: int
    expanded_score_attempts: int
    unsafe_score_timeouts: int
    component_statuses: tuple[tuple[str, int], ...]

    def payload(self) -> dict[str, object]:
        return {
            "episodes": self.episodes,
            "graphs_by_order": dict(self.graphs_by_order),
            "proposals": self.proposals,
            "no_proposals": self.no_proposals,
            "accepted_rewrites": self.accepted_rewrites,
            "score_attempts": self.score_attempts,
            "unique_graph_scores": self.unique_graph_scores,
            "expanded_score_attempts": self.expanded_score_attempts,
            "unsafe_score_timeouts": self.unsafe_score_timeouts,
            "component_statuses": dict(self.component_statuses),
        }


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    score: RationalInterval
    diagnostics: EvaluationDiagnostics
    worker: dict[str, object]


class IndependentEvaluator:
    def evaluate(self, graphs: tuple[Graph, ...], policy: Policy) -> EvaluationResult:
        if not graphs:
            raise EvaluationFailure("evaluation requires at least one graph")
        for graph in graphs:
            try:
                graph.validate_scientific_invariants()
            except ValueError as exc:
                raise InvalidGraphError(
                    f"invalid evaluation graph {graph.graph_hash}: {exc}"
                ) from exc
            if graph.order > 128:
                raise InvalidGraphError("the authoritative HEG scorer supports order at most 128")

        auc_by_order: dict[int, list[RationalInterval]] = defaultdict(list)
        proposals = 0
        no_proposals = 0
        accepted = 0
        with ScoreWorker() as worker:
            scorer = _EvidenceScorer(worker)
            for graph in graphs:
                scale = EnergyScale.build(
                    graph.order,
                    tuple(int(length) for length in forbidden_lengths(graph.order)),
                    policy.witness_cap,
                )
                current = graph
                try:
                    current_evidence = scorer.score(current, policy.witness_cap)
                except ScoreTimeoutWithoutPartial:
                    scorer.unsafe_score_timeouts += 1
                    auc_by_order[graph.order].append(RationalInterval(Fraction(), Fraction(1)))
                    continue
                trajectory = [scale.utility(scale.interval(current_evidence))]
                unsafe_timeout = False
                for step_index in range(policy.horizon):
                    try:
                        candidate = policy.propose(current, step_index=step_index)
                    except Exception as exc:
                        raise BaselineFailure(
                            f"baseline {policy.name} failed at step {step_index}: {exc}"
                        ) from exc
                    if candidate is None:
                        no_proposals += 1
                        trajectory.append(scale.utility(scale.interval(current_evidence)))
                        continue
                    proposals += 1
                    try:
                        candidate.validate_scientific_invariants()
                    except ValueError as exc:
                        raise BaselineFailure(
                            f"baseline {policy.name} proposed an invalid graph: {exc}"
                        ) from exc
                    try:
                        candidate_evidence = scorer.score(candidate, policy.witness_cap)
                    except ScoreTimeoutWithoutPartial:
                        scorer.unsafe_score_timeouts += 1
                        unsafe_timeout = True
                        break
                    incumbent_energy = scale.interval(current_evidence)
                    candidate_energy = scale.interval(candidate_evidence)
                    overlaps = not (
                        candidate_energy.upper < incumbent_energy.lower
                        or incumbent_energy.upper < candidate_energy.lower
                    )
                    if overlaps and (not incumbent_energy.exact or not candidate_energy.exact):
                        current_evidence = scorer.expand(current, current_evidence)
                        candidate_evidence = scorer.expand(candidate, candidate_evidence)
                        incumbent_energy = scale.interval(current_evidence)
                        candidate_energy = scale.interval(candidate_evidence)
                    if candidate_energy.upper < incumbent_energy.lower:
                        current = candidate
                        current_evidence = candidate_evidence
                        accepted += 1
                    trajectory.append(scale.utility(scale.interval(current_evidence)))
                auc_by_order[graph.order].append(
                    RationalInterval(Fraction(), Fraction(1))
                    if unsafe_timeout
                    else _mean(_best_so_far(trajectory))
                )
            if worker.identity is None:
                raise EvaluationFailure("score worker identity is unavailable")
            score = _mean(_mean(tuple(auc_by_order[order])) for order in sorted(auc_by_order))
            diagnostics = EvaluationDiagnostics(
                episodes=len(graphs),
                graphs_by_order=tuple(
                    sorted(Counter(graph.order for graph in graphs).items())
                ),
                proposals=proposals,
                no_proposals=no_proposals,
                accepted_rewrites=accepted,
                score_attempts=scorer.score_attempts,
                unique_graph_scores=scorer.unique_graph_scores,
                expanded_score_attempts=scorer.expanded_score_attempts,
                unsafe_score_timeouts=scorer.unsafe_score_timeouts,
                component_statuses=tuple(sorted(scorer.statuses.items())),
            )
            return EvaluationResult(score, diagnostics, worker.identity.payload())


class _EvidenceScorer:
    def __init__(self, worker: ScoreWorker) -> None:
        self.worker = worker
        self.cache: dict[tuple[str, tuple[int, ...], int, int], ScoreEvidence] = {}
        self.score_attempts = 0
        self.unique_graph_scores = 0
        self.expanded_score_attempts = 0
        self.unsafe_score_timeouts = 0
        self.statuses: Counter[str] = Counter()

    def score(self, graph: Graph, witness_cap: int) -> ScoreEvidence:
        lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
        return self._score(graph, lengths, witness_cap, INITIAL_NODE_BUDGET)

    def expand(self, graph: Graph, evidence: ScoreEvidence) -> ScoreEvidence:
        lengths = tuple(
            component.length for component in evidence.components if not component.exact
        )
        if not lengths:
            return evidence
        self.expanded_score_attempts += 1
        try:
            expanded = self._score(
                graph,
                lengths,
                evidence.witness_cap,
                EXPANDED_NODE_BUDGET,
            )
        except ScoreTimeoutWithoutPartial:
            self.unsafe_score_timeouts += 1
            return evidence
        replacements = {component.length: component for component in expanded.components}
        merged: list[CycleEvidence] = []
        for component in evidence.components:
            candidate = replacements.get(component.length, component)
            if candidate.lower < component.lower or candidate.upper > component.upper:
                raise EvaluationFailure("expanded score evidence weakened a sound bound")
            merged.append(candidate)
        return replace(evidence, components=tuple(merged))

    def _score(
        self,
        graph: Graph,
        lengths: tuple[int, ...],
        witness_cap: int,
        node_budget: int,
    ) -> ScoreEvidence:
        key = (graph.graph_hash, lengths, witness_cap, node_budget)
        self.score_attempts += 1
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        self.unique_graph_scores += 1
        bit_graph = BitGraph.from_edges(graph.order, graph.edges)
        response = self.worker.score(
            bit_graph,
            lengths=lengths,
            witness_cap=witness_cap,
            node_budget=node_budget,
        )
        by_length = {int(result.length): result for result in response.results}
        if set(by_length) != set(lengths):
            raise EvaluationFailure("HEG scorer omitted or added a forbidden length")
        components = []
        for length in lengths:
            result = by_length[length]
            raw_count = int(result.count)
            if raw_count >= witness_cap:
                observed = lower = upper = witness_cap
                status = "SATURATED_AT_CAP"
            elif bool(result.complete):
                observed = lower = upper = raw_count
                status = "EXACT"
            else:
                observed = lower = raw_count
                upper = witness_cap
                status = "SEARCH_BUDGET_EXHAUSTED"
            self.statuses[status] += 1
            components.append(
                CycleEvidence(
                    length,
                    observed,
                    lower,
                    upper,
                    status,
                    node_budget,
                    int(result.nodes),
                )
            )
        evidence = ScoreEvidence(
            graph.graph_hash,
            graph.order,
            len(graph.edges),
            witness_cap,
            tuple(components),
        )
        self.cache[key] = evidence
        return evidence


def _weight(length: int) -> int:
    return max(1, 64 // length)


def _best_so_far(values: Iterable[RationalInterval]) -> tuple[RationalInterval, ...]:
    lower: Fraction | None = None
    upper: Fraction | None = None
    result = []
    for value in values:
        lower = value.lower if lower is None else max(lower, value.lower)
        upper = value.upper if upper is None else max(upper, value.upper)
        result.append(RationalInterval(lower, upper))
    return tuple(result)


def _mean(values: Iterable[RationalInterval]) -> RationalInterval:
    materialized = tuple(values)
    if not materialized:
        raise EvaluationFailure("cannot average an empty scientific sample")
    count = len(materialized)
    return RationalInterval(
        sum((value.lower for value in materialized), Fraction()) / count,
        sum((value.upper for value in materialized), Fraction()) / count,
    )


def _fraction_payload(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def score_payload(score: RationalInterval) -> dict[str, object]:
    return {
        "kind": "conservative_interval_fitness",
        "fitness": score.payload(),
        "exact": score.exact,
    }


def score_from_payload(payload: Mapping[str, object]) -> RationalInterval:
    fitness = payload["fitness"]
    if not isinstance(fitness, Mapping):
        raise ValueError("score fitness must be an object")
    return RationalInterval(
        _fraction_from_payload(fitness["lower"]),
        _fraction_from_payload(fitness["upper"]),
    )


def _fraction_from_payload(payload: object) -> Fraction:
    if not isinstance(payload, Mapping):
        raise ValueError("fraction must be an object")
    numerator = payload["numerator"]
    denominator = payload["denominator"]
    if not isinstance(numerator, int) or not isinstance(denominator, int):
        raise ValueError("fraction fields must be integers")
    return Fraction(numerator, denominator)
