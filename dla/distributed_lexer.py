"""
Distributed lexical analysis via DFA transition-function summaries.

Pipeline
--------
Phase 1 (parallel, one process per chunk):
    For a chunk starting at ANY possible DFA state q, simulate the chunk and
    record where it ends up. This gives a summary f_i : Q -> Q (plus, for
    each q, the absolute offset at which a still-open token/comment/string
    run visible within that chunk began). No scanning/boundary-search is
    needed up front -- chunks are cut at arbitrary, evenly spaced offsets.

Phase 2 (coordinator, cheap): compose the N summaries with an inclusive
    prefix scan to learn the TRUE incoming state of every chunk. Composition
    of two summaries is associative, so this can be done as a strictly
    sequential left-to-right fold (depth N-1) or as a Hillis-Steele parallel
    scan (depth ceil(log2 N)). Both are implemented so their cost can be
    compared.

Phase 3 (parallel, one process per chunk): re-simulate each chunk ONE more
    time, this time starting from its now-known true state, actually
    emitting tokens with correct absolute offsets (reconstructing any token
    that began in an earlier chunk by slicing the full source).
"""

import time
from dataclasses import dataclass
from multiprocessing import Pool
from typing import List, Optional, Tuple

import automata
import sequential_lexer
from sequential_lexer import Token, tokenize_core, attach_positions


@dataclass
class ChunkSummary:
    end: List[int]                     # end[q] = state after this chunk, entering at q
    open: List[Optional[int]]          # open[q] = absolute start of a run open at chunk end


def naive_chunks(n_total: int, n_workers: int) -> List[Tuple[int, int]]:
    base, rem = divmod(n_total, n_workers)
    bounds = []
    start = 0
    for w in range(n_workers):
        size = base + (1 if w < rem else 0)
        bounds.append((start, start + size))
        start += size
    return bounds


# --------------------------------------------------------------- Phase 1

def _phase1_worker(args):
    chunk_text, start_offset, dfa, skip_tokens, at_eof = args
    end = [0] * dfa.n_states
    openarr: List[Optional[int]] = [None] * dfa.n_states
    for q in range(dfa.n_states):
        _, es, os_ = tokenize_core(chunk_text, dfa, skip_tokens,
                                    start_offset=start_offset, start_state=q, at_eof=at_eof)
        end[q] = es
        openarr[q] = os_
    return ChunkSummary(end=end, open=openarr)


def phase1_build_summaries(full_source: str, bounds, dfa, skip_tokens, pool: Pool) -> List[ChunkSummary]:
    last = len(bounds) - 1
    args = [(full_source[s:e], s, dfa, skip_tokens, i == last) for i, (s, e) in enumerate(bounds)]
    return pool.map(_phase1_worker, args)


# --------------------------------------------------------------- Phase 2

def combine(a: ChunkSummary, b: ChunkSummary, dfa) -> ChunkSummary:
    """a happens first, then b. Returns the summary of a-then-b as a whole."""
    n = dfa.n_states
    end = [0] * n
    openarr: List[Optional[int]] = [None] * n
    for q in range(n):
        mid = a.end[q]
        end[q] = b.end[mid]
        if b.open[mid] is not None:
            openarr[q] = b.open[mid]
        elif b.end[mid] != dfa.start:
            openarr[q] = a.open[q]      # b never reset: run predates b entirely
        else:
            openarr[q] = None
    return ChunkSummary(end=end, open=openarr)


def identity_summary(dfa) -> ChunkSummary:
    return ChunkSummary(end=list(range(dfa.n_states)), open=[None] * dfa.n_states)


def serial_scan(summaries: List[ChunkSummary], dfa) -> List[ChunkSummary]:
    """Inclusive prefix scan via a strictly sequential left fold. Sequential
    depth: N combine() calls, each depending on the previous one's result."""
    result = []
    acc = identity_summary(dfa)
    for s in summaries:
        acc = combine(acc, s, dfa)
        result.append(acc)
    return result


def parallel_scan(summaries: List[ChunkSummary], dfa) -> Tuple[List[ChunkSummary], int]:
    """Inclusive prefix scan via Hillis-Steele doubling. Sequential depth:
    ceil(log2(N)) rounds; within a round every combine() is independent of
    every other combine() in that round. Returns (result, n_rounds)."""
    result = list(summaries)
    n = len(result)
    d = 1
    rounds = 0
    while d < n:
        new_result = list(result)
        for i in range(d, n):
            new_result[i] = combine(result[i - d], result[i], dfa)
        result = new_result
        d *= 2
        rounds += 1
    return result, rounds


def true_states_from_scan(scan_result: List[ChunkSummary], dfa):
    """scan_result[i] = summary of chunks[0..i] starting from file-start.
    Returns parallel lists: true_start_state[i], true_open_start[i] for
    entering chunk i (index 0 = file's true start, dfa.start, no open run)."""
    n = len(scan_result)
    true_start_state = [dfa.start] * n
    true_open_start = [None] * n
    for i in range(1, n):
        prev = scan_result[i - 1]
        true_start_state[i] = prev.end[dfa.start]
        true_open_start[i] = prev.open[dfa.start]
    final_state = scan_result[-1].end[dfa.start] if n else dfa.start
    return true_start_state, true_open_start, final_state


# --------------------------------------------------------------- Phase 3

def _phase3_worker(args):
    chunk_text, start_offset, dfa, skip_tokens, start_state, open_start, full_source, at_eof = args
    tokens, end_state, _ = tokenize_core(
        chunk_text, dfa, skip_tokens, start_offset=start_offset,
        start_state=start_state, inherited_open_start=open_start, full_source=full_source, at_eof=at_eof)
    return tokens


def phase3_tokenize(full_source: str, bounds, dfa, skip_tokens,
                     true_start_state, true_open_start, pool: Pool) -> List[Token]:
    last = len(bounds) - 1
    args = [
        (full_source[s:e], s, dfa, skip_tokens, true_start_state[i], true_open_start[i],
         full_source, i == last)
        for i, (s, e) in enumerate(bounds)
    ]
    chunk_results = pool.map(_phase3_worker, args)
    all_tokens: List[Token] = []
    for toks in chunk_results:
        all_tokens.extend(toks)
    return all_tokens


# --------------------------------------------------------------- orchestration

@dataclass
class DistributedRunResult:
    tokens: List[Token]
    timings: dict
    n_rounds_parallel_scan: int


def run_distributed(full_source: str, n_workers: int, dfa, skip_tokens, pool: Pool,
                     scan_method: str = "parallel") -> DistributedRunResult:
    bounds = naive_chunks(len(full_source), n_workers)

    t0 = time.perf_counter()
    summaries = phase1_build_summaries(full_source, bounds, dfa, skip_tokens, pool)
    t1 = time.perf_counter()

    if scan_method == "serial":
        scan_result = serial_scan(summaries, dfa)
        n_rounds = len(summaries)
    else:
        scan_result, n_rounds = parallel_scan(summaries, dfa)
    t2 = time.perf_counter()

    true_start_state, true_open_start, final_state = true_states_from_scan(scan_result, dfa)
    tokens = phase3_tokenize(full_source, bounds, dfa, skip_tokens,
                              true_start_state, true_open_start, pool)
    if final_state != dfa.start:
        tokens.append(Token("ERROR_UNTERMINATED", "", len(full_source), len(full_source)))
    t3 = time.perf_counter()

    attach_positions(full_source, tokens)
    t4 = time.perf_counter()

    timings = {
        "phase1_s": t1 - t0,
        "phase2_scan_s": t2 - t1,
        "phase3_s": t3 - t2,
        "positions_s": t4 - t3,
        "total_s": t4 - t0,
    }
    return DistributedRunResult(tokens=tokens, timings=timings, n_rounds_parallel_scan=n_rounds)


def run_naive_wrong(full_source: str, n_workers: int, dfa, skip_tokens, pool: Pool) -> List[Token]:
    """Baseline for comparison: split at the same arbitrary offsets but tokenize
    every chunk assuming it starts at dfa.start, with no state propagation and
    no cross-chunk reconstruction. Demonstrates why naive splitting is wrong."""
    bounds = naive_chunks(len(full_source), n_workers)
    last = len(bounds) - 1
    args = [(full_source[s:e], s, dfa, skip_tokens, dfa.start, None, None, i == last)
            for i, (s, e) in enumerate(bounds)]
    chunk_results = pool.map(_phase3_worker, args)
    tokens: List[Token] = []
    for toks in chunk_results:
        tokens.extend(toks)
    return tokens
