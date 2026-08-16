#!/usr/bin/env python3
"""
Monotone HEG growth probe.

Start from one valid HEG graph and repeatedly increase its order by exactly one.
At each step, attach the new vertex to exactly three existing vertices, score all
(or as many as the budget permits) distinct attachments in parallel, and commit
the best candidate.

Run from the Graphoratory repository/environment, for example:

    uv run python /path/to/heg_growth_probe.py \
        --start-order 4 --target-order 8 --step-seconds 60 --workers 8

or:

    uv run python /path/to/heg_growth_probe.py \
        --start-order 4 --steps 20 --step-seconds 30 \
        --total-seconds 600 --workers 8

The script reuses Graphoratory's Graph/generator and bundled HEG score worker.
It keeps the evolving graph in memory and writes nothing unless it finds an
exact zero-forbidden-cycle graph or --save-final is supplied.
"""

from __future__ import annotations

import argparse
import atexit
import itertools
import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.table import Table
from sglab.model import BitGraph  # type: ignore[import-untyped]
from sglab.targets.erdos_gyarfas import forbidden_lengths  # type: ignore[import-untyped]

from graphoratory.config import (
    DegreeRangeConfig,
    ErdosRenyiConfig,
    GraphConfig,
    MixedGeneratorConfig,
)
from graphoratory.graphs import Graph, generate_graphs
from graphoratory.science.worker import ScoreWorker

MAX_ORDER = 128
DEFAULT_WITNESS_CAP = 1_000_000
DEFAULT_NODE_BUDGET = 2_000_000

STATUS_EXACT = "EXACT"
STATUS_SATURATED = "SATURATED_AT_CAP"
STATUS_BUDGET = "SEARCH_BUDGET_EXHAUSTED"

console = Console()
_tls = threading.local()
_workers_lock = threading.Lock()
_live_score_workers: list[ScoreWorker] = []


@dataclass(frozen=True, slots=True)
class ComponentScore:
    length: int
    observed: int
    lower: int
    upper: int
    status: str
    nodes: int
    elapsed_ns: int

    @property
    def exact(self) -> bool:
        return self.status == STATUS_EXACT


@dataclass(frozen=True, slots=True)
class GraphScore:
    graph: Graph
    components: tuple[ComponentScore, ...]
    attachment: tuple[int, int, int] | None
    elapsed_seconds: float

    @property
    def fully_exact(self) -> bool:
        return all(component.exact for component in self.components)

    @property
    def lower_total(self) -> int:
        return sum(component.lower for component in self.components)

    @property
    def upper_total(self) -> int:
        return sum(component.upper for component in self.components)

    @property
    def lower_weighted(self) -> int:
        return sum(weight(component.length) * component.lower for component in self.components)

    @property
    def upper_weighted(self) -> int:
        return sum(weight(component.length) * component.upper for component in self.components)

    @property
    def non_exact_components(self) -> int:
        return sum(not component.exact for component in self.components)

    def component_map(self) -> dict[int, ComponentScore]:
        return {component.length: component for component in self.components}


def weight(length: int) -> int:
    # Same short-cycle weight currently used by Graphoratory's evaluator.
    return max(1, 64 // length)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Grow a minimum-degree-3 graph by one vertex per step and search "
            "3-neighbour attachments that minimise HEG forbidden cycles."
        )
    )
    parser.add_argument("--start-order", type=int, default=4)
    parser.add_argument(
        "--start-graph",
        type=Path,
        default=None,
        help="Optional JSON with {order, edges}; overrides generated start graph.",
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument("--target-order", type=int, default=None)
    target.add_argument(
        "--steps",
        type=int,
        default=None,
        help="Number of order-increase steps; 4 -> 8 means --steps 4.",
    )

    parser.add_argument(
        "--step-seconds",
        type=float,
        default=60.0,
        help="Wall-clock search budget per order increment; 0 = unlimited.",
    )
    parser.add_argument(
        "--total-seconds",
        type=float,
        default=0.0,
        help="Wall-clock budget for the whole run; 0 = unlimited.",
    )
    parser.add_argument(
        "--max-trials-per-step",
        type=int,
        default=0,
        help="Optional candidate-count limit per step; 0 = unlimited.",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=4001)
    parser.add_argument(
        "--node-budget",
        type=int,
        default=DEFAULT_NODE_BUDGET,
        help="HEG scorer DFS-node budget per forbidden length.",
    )
    parser.add_argument(
        "--witness-cap",
        type=int,
        default=DEFAULT_WITNESS_CAP,
        help=(
            "Emergency scorer cap. Default is deliberately high so small-order "
            "runs behave effectively uncapped."
        ),
    )
    parser.add_argument(
        "--objective",
        choices=("current", "lex"),
        default="current",
        help=(
            "current: minimise total forbidden cycles, then current HEG weighted "
            "total; lex: minimise C4, then C8, then C16, ..."
        ),
    )
    parser.add_argument(
        "--allow-inexact",
        action="store_true",
        help="Allow committing a candidate with saturated/budget-exhausted components.",
    )
    parser.add_argument(
        "--save-final",
        type=Path,
        default=None,
        help="Optional path for final graph JSON.",
    )
    parser.add_argument(
        "--hit-dir",
        type=Path,
        default=Path("growth_hits"),
        help="Created only if an exact zero-forbidden-cycle graph is found.",
    )

    args = parser.parse_args()
    if args.start_order < 4:
        parser.error("--start-order must be >= 4")
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.node_budget < 1:
        parser.error("--node-budget must be >= 1")
    if args.witness_cap < 2:
        parser.error("--witness-cap must be >= 2")
    if args.step_seconds < 0 or args.total_seconds < 0:
        parser.error("time budgets must be >= 0")
    if args.max_trials_per_step < 0:
        parser.error("--max-trials-per-step must be >= 0")

    if args.target_order is None and args.steps is None:
        args.target_order = 8
    if args.steps is not None:
        if args.steps < 0:
            parser.error("--steps must be >= 0")
        args.target_order = args.start_order + args.steps
    if args.target_order is None:
        parser.error("could not resolve target order")
    if args.target_order < args.start_order:
        parser.error("--target-order must be >= --start-order")
    if args.target_order > MAX_ORDER:
        parser.error(f"current bundled HEG scorer supports order <= {MAX_ORDER}")
    return args


def _thread_worker_init() -> None:
    worker = ScoreWorker()
    worker.__enter__()
    _tls.score_worker = worker
    with _workers_lock:
        _live_score_workers.append(worker)


def _close_workers() -> None:
    with _workers_lock:
        workers = list(_live_score_workers)
        _live_score_workers.clear()
    for worker in workers:
        try:
            worker.close()
        except Exception:
            pass


atexit.register(_close_workers)


def _score_worker() -> ScoreWorker:
    worker = getattr(_tls, "score_worker", None)
    if worker is None:
        raise RuntimeError("score worker thread was not initialized")
    return worker


def load_or_generate_start_graph(args: argparse.Namespace) -> Graph:
    if args.start_graph is not None:
        payload = json.loads(args.start_graph.read_text(encoding="utf-8"))
        graph = Graph.from_edges(
            int(payload["order"]),
            ((int(edge[0]), int(edge[1])) for edge in payload["edges"]),
        )
        if graph.order != args.start_order:
            raise ValueError(
                f"--start-graph has order {graph.order}, but --start-order={args.start_order}"
            )
        graph.validate_scientific_invariants(max_order=MAX_ORDER)
        return graph

    config = GraphConfig(
        generator="cycle_matching_stub_pairing",
        workspace_graph_count=1,
        line_graph_count=1,
        min_order=args.start_order,
        max_order=args.start_order,
        seed=args.seed,
        random_regular=DegreeRangeConfig(degree_min=3, degree_max=4),
        erdos_renyi_rejection=ErdosRenyiConfig(
            expected_degree_min=3.0,
            expected_degree_max=4.0,
        ),
        degree_sequence_rejection=DegreeRangeConfig(degree_min=3, degree_max=4),
        mixed=MixedGeneratorConfig(
            generators=("cycle_matching_stub_pairing",),
            weights=(1.0,),
        ),
    )
    graph = generate_graphs(config).graphs[0]
    graph.validate_scientific_invariants(max_order=MAX_ORDER)
    return graph


def attach_new_vertex(graph: Graph, attachment: tuple[int, int, int]) -> Graph:
    new_vertex = graph.order
    edges = list(graph.edges)
    edges.extend((old_vertex, new_vertex) for old_vertex in attachment)
    candidate = Graph.from_edges(graph.order + 1, edges)
    candidate.validate_scientific_invariants(max_order=MAX_ORDER)
    return candidate


def score_graph(
    graph: Graph,
    *,
    attachment: tuple[int, int, int] | None,
    witness_cap: int,
    node_budget: int,
) -> GraphScore:
    started = time.perf_counter()
    lengths = tuple(int(length) for length in forbidden_lengths(graph.order))
    bit_graph = BitGraph.from_edges(graph.order, graph.edges)
    response = _score_worker().score(
        bit_graph,
        lengths=lengths,
        witness_cap=witness_cap,
        node_budget=node_budget,
    )

    by_length = {int(result.length): result for result in response.results}
    if set(by_length) != set(lengths):
        raise RuntimeError(
            f"scorer returned lengths {sorted(by_length)} but expected {list(lengths)}"
        )

    components: list[ComponentScore] = []
    for length in lengths:
        result = by_length[length]
        raw_count = int(result.count)
        if raw_count >= witness_cap:
            observed = lower = upper = witness_cap
            status = STATUS_SATURATED
        elif bool(result.complete):
            observed = lower = upper = raw_count
            status = STATUS_EXACT
        else:
            observed = lower = raw_count
            upper = witness_cap
            status = STATUS_BUDGET
        components.append(
            ComponentScore(
                length=length,
                observed=observed,
                lower=lower,
                upper=upper,
                status=status,
                nodes=int(result.nodes),
                elapsed_ns=int(result.elapsed_ns),
            )
        )

    return GraphScore(
        graph=graph,
        components=tuple(components),
        attachment=attachment,
        elapsed_seconds=time.perf_counter() - started,
    )


def evaluate_attachment(
    base: Graph,
    attachment: tuple[int, int, int],
    witness_cap: int,
    node_budget: int,
) -> GraphScore:
    return score_graph(
        attach_new_vertex(base, attachment),
        attachment=attachment,
        witness_cap=witness_cap,
        node_budget=node_budget,
    )


def score_key(result: GraphScore, objective: str) -> tuple[int, ...]:
    # Conservative: an incomplete component uses its upper bound (the cap), so
    # budget exhaustion is never rewarded merely because few witnesses were seen.
    if objective == "lex":
        upper_vector = tuple(component.upper for component in result.components)
        lower_vector = tuple(component.lower for component in result.components)
        return (
            *upper_vector,
            result.non_exact_components,
            *lower_vector,
            len(result.graph.edges),
        )
    return (
        result.upper_total,
        result.upper_weighted,
        result.non_exact_components,
        result.lower_total,
        result.lower_weighted,
        len(result.graph.edges),
    )


def status_summary(result: GraphScore) -> str:
    if result.fully_exact:
        return "exact"
    sat = [f"C{c.length}" for c in result.components if c.status == STATUS_SATURATED]
    budget = [f"C{c.length}" for c in result.components if c.status == STATUS_BUDGET]
    parts: list[str] = []
    if sat:
        parts.append("sat:" + ",".join(sat))
    if budget:
        parts.append("budget:" + ",".join(budget))
    return " ".join(parts)


def counts_text(result: GraphScore) -> str:
    parts: list[str] = []
    for component in result.components:
        if component.status == STATUS_EXACT:
            text = str(component.observed)
        elif component.status == STATUS_SATURATED:
            text = f">={component.observed}"
        else:
            text = f"[{component.lower},{component.upper}]"
        parts.append(f"C{component.length}={text}")
    return " ".join(parts)


def delta_text(previous: GraphScore, current: GraphScore) -> str:
    previous_map = previous.component_map()
    pieces: list[str] = []
    for component in current.components:
        if not component.exact:
            pieces.append(f"C{component.length}=?")
            continue
        old = previous_map.get(component.length)
        old_value = old.observed if old is not None and old.exact else 0
        delta = component.observed - old_value
        pieces.append(f"C{component.length}{delta:+d}")
    return " ".join(pieces)


def make_candidate_order(
    order: int,
    *,
    seed: int,
    max_trials: int,
) -> list[tuple[int, int, int]]:
    # Unique by construction: exactly C(order, 3) possible attachments.
    candidates = list(itertools.combinations(range(order), 3))
    rng = random.Random((seed << 32) ^ order ^ 0x9E3779B97F4A7C15)
    rng.shuffle(candidates)
    if max_trials:
        del candidates[max_trials:]
    return candidates


def print_header(args: argparse.Namespace, start: Graph) -> None:
    console.print(
        f"[bold]HEG monotone growth probe[/bold]  "
        f"start={start.order} target={args.target_order} workers={args.workers} "
        f"objective={args.objective}"
    )
    console.print(
        f"step_budget={args.step_seconds or 'unlimited'}s  "
        f"total_budget={args.total_seconds or 'unlimited'}s  "
        f"node_budget={args.node_budget:,}  emergency_cap={args.witness_cap:,}"
    )
    console.print(
        "[dim]Pure addition cannot destroy existing cycles; this probe measures how "
        "slowly forbidden-cycle pressure can grow, not whether counts can decrease.[/dim]"
    )


def print_step_table(rows: Iterable[dict[str, str]]) -> None:
    table = Table(show_edge=False, padding=(0, 1))
    table.add_column("STEP", justify="right")
    table.add_column("ORDER", justify="right")
    table.add_column("TRIED", justify="right")
    table.add_column("SPACE", justify="right")
    table.add_column("SCORE")
    table.add_column("DELTA")
    table.add_column("STATE")
    table.add_column("ATTACH")
    table.add_column("TIME", justify="right")
    table.add_column("HASH")
    for row in rows:
        table.add_row(
            row["step"], row["order"], row["tried"], row["space"],
            row["score"], row["delta"], row["state"], row["attach"],
            row["time"], row["hash"],
        )
    console.print(table)


def save_graph(path: Path, graph: Graph, score: GraphScore) -> None:
    payload = {
        **graph.record(),
        "forbidden_cycle_score": {
            str(component.length): {
                "observed": component.observed,
                "lower": component.lower,
                "upper": component.upper,
                "status": component.status,
            }
            for component in score.components
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def maybe_save_hit(args: argparse.Namespace, result: GraphScore) -> Path | None:
    if not result.fully_exact or result.lower_total != 0:
        return None
    args.hit_dir.mkdir(parents=True, exist_ok=True)
    path = args.hit_dir / f"heg-zero-order-{result.graph.order}-{result.graph.graph_hash[:8]}.json"
    save_graph(path, result.graph, result)
    return path


def main() -> int:
    args = parse_args()
    start_graph = load_or_generate_start_graph(args)
    print_header(args, start_graph)

    global_started = time.perf_counter()
    global_deadline = global_started + args.total_seconds if args.total_seconds > 0 else math.inf

    with ThreadPoolExecutor(
        max_workers=args.workers,
        initializer=_thread_worker_init,
        thread_name_prefix="heg-score",
    ) as executor:
        current_score = executor.submit(
            score_graph,
            start_graph,
            attachment=None,
            witness_cap=args.witness_cap,
            node_budget=args.node_budget,
        ).result()
        current_graph = start_graph

        print_step_table([
            {
                "step": "0",
                "order": str(current_graph.order),
                "tried": "-",
                "space": "-",
                "score": counts_text(current_score),
                "delta": "-",
                "state": status_summary(current_score),
                "attach": "-",
                "time": f"{current_score.elapsed_seconds:.3f}s",
                "hash": current_graph.graph_hash[:8],
            }
        ])

        hit_path = maybe_save_hit(args, current_score)
        if hit_path is not None:
            console.print(f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] saved: {hit_path}")
            return 0

        for step in range(1, args.target_order - args.start_order + 1):
            if time.perf_counter() >= global_deadline:
                console.print("[yellow]Total time budget exhausted before next growth step.[/yellow]")
                break

            base = current_graph
            next_order = base.order + 1
            full_space = math.comb(base.order, 3)
            candidates = make_candidate_order(
                base.order,
                seed=args.seed + step,
                max_trials=args.max_trials_per_step,
            )

            step_started = time.perf_counter()
            step_deadline = min(
                global_deadline,
                step_started + args.step_seconds if args.step_seconds > 0 else math.inf,
            )

            best: GraphScore | None = None
            evaluated = 0

            # One batch per scorer thread. Candidate enumeration is duplicate-free,
            # so no shared dedupe hash table is needed.
            for offset in range(0, len(candidates), args.workers):
                if time.perf_counter() >= step_deadline:
                    break

                batch = candidates[offset : offset + args.workers]
                futures = [
                    executor.submit(
                        evaluate_attachment,
                        base,
                        attachment,
                        args.witness_cap,
                        args.node_budget,
                    )
                    for attachment in batch
                ]

                for future in futures:
                    result = future.result()
                    evaluated += 1
                    if best is None or score_key(result, args.objective) < score_key(best, args.objective):
                        best = result

                if time.perf_counter() >= step_deadline:
                    break

            elapsed = time.perf_counter() - step_started
            if best is None:
                console.print(
                    f"[yellow]Step {step}: no candidate completed before the budget expired.[/yellow]"
                )
                break

            if not best.fully_exact and not args.allow_inexact:
                console.print(
                    f"[bold yellow]Step {step} stopped:[/bold yellow] best available candidate "
                    f"is not exact ({status_summary(best)}). Increase --node-budget/"
                    "--step-seconds or use --allow-inexact."
                )
                print_step_table([
                    {
                        "step": str(step),
                        "order": str(next_order),
                        "tried": str(evaluated),
                        "space": str(full_space),
                        "score": counts_text(best),
                        "delta": delta_text(current_score, best),
                        "state": "NOT COMMITTED " + status_summary(best),
                        "attach": str(best.attachment),
                        "time": f"{elapsed:.2f}s",
                        "hash": best.graph.graph_hash[:8],
                    }
                ])
                break

            previous_score = current_score
            current_score = best
            current_graph = best.graph

            print_step_table([
                {
                    "step": str(step),
                    "order": str(current_graph.order),
                    "tried": str(evaluated),
                    "space": str(full_space),
                    "score": counts_text(current_score),
                    "delta": delta_text(previous_score, current_score),
                    "state": status_summary(current_score),
                    "attach": str(current_score.attachment),
                    "time": f"{elapsed:.2f}s",
                    "hash": current_graph.graph_hash[:8],
                }
            ])

            if evaluated < len(candidates):
                console.print(
                    f"[dim]Step budget stopped search after {evaluated}/{len(candidates)} "
                    "scheduled candidates.[/dim]"
                )

            hit_path = maybe_save_hit(args, current_score)
            if hit_path is not None:
                console.print(
                    f"[bold red]ZERO FORBIDDEN CYCLES[/bold red] "
                    f"order={current_graph.order} saved: {hit_path}"
                )
                return 0

    if args.save_final is not None:
        save_graph(args.save_final, current_graph, current_score)
        console.print(f"Final graph saved: {args.save_final}")

    total_elapsed = time.perf_counter() - global_started
    console.print(
        f"[bold]Done[/bold] order={current_graph.order} "
        f"{counts_text(current_score)} elapsed={total_elapsed:.2f}s "
        f"hash={current_graph.graph_hash[:8]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
