"""
Runs the full experiment suite and writes results/results.json +
results/results.csv + results/scan_depth.csv. Run with: python3 benchmark.py
"""

import csv
import json
import math
import os
import time
from multiprocessing import Pool

import distributed_lexer as dl
import lexer_build
import sequential_lexer as sl
import sourcegen

RESULTS_DIR = "results"
WORKER_COUNTS = [1, 2, 4, 8, 16]


def tok_tuple(t):
    return (t.type, t.lexeme, t.start, t.end)


def bench_file(name, path, dfa, skip, pool, rows):
    with open(path) as f:
        text = f.read()
    size = len(text)

    t0 = time.perf_counter()
    oracle = sl.tokenize(text, dfa, skip)
    t1 = time.perf_counter()
    seq_time = t1 - t0
    oracle_tuples = [tok_tuple(t) for t in oracle]
    print(f"[{name}] size={size} chars, sequential tokens={len(oracle)}, "
          f"sequential time={seq_time*1000:.2f} ms")

    for n_workers in WORKER_COUNTS:
        if n_workers > size:
            continue
        for method in ("serial", "parallel"):
            result = dl.run_distributed(text, n_workers, dfa, skip, pool, scan_method=method)
            got_tuples = [tok_tuple(t) for t in result.tokens]
            correct = got_tuples == oracle_tuples
            row = dict(
                file=name, size_chars=size, n_workers=n_workers, scan_method=method,
                sequential_s=seq_time, correct=correct,
                n_tokens=len(result.tokens),
                scan_rounds=result.n_rounds_parallel_scan,
                speedup_vs_sequential=seq_time / result.timings["total_s"] if result.timings["total_s"] else float("nan"),
                **{f"distributed_{k}": v for k, v in result.timings.items()},
            )
            rows.append(row)
            print(f"  workers={n_workers:>2} scan={method:<8} correct={correct} "
                  f"total={result.timings['total_s']*1000:7.2f} ms "
                  f"(phase1={result.timings['phase1_s']*1000:6.2f}, "
                  f"scan={result.timings['phase2_scan_s']*1000:6.3f}, "
                  f"phase3={result.timings['phase3_s']*1000:6.2f}) "
                  f"speedup={row['speedup_vs_sequential']:.2f}x")


def bench_naive_wrong(dfa, skip, pool, path):
    with open(path) as f:
        text = f.read()
    oracle_tuples = [tok_tuple(t) for t in sl.tokenize(text, dfa, skip)]
    report = []
    for n_workers in (2, 4, 8):
        naive_tuples = [tok_tuple(t) for t in dl.run_naive_wrong(text, n_workers, dfa, skip, pool)]
        agree = naive_tuples == oracle_tuples
        n_mismatched_tokens = sum(
            1 for a, b in zip(naive_tuples, oracle_tuples) if a != b
        ) + abs(len(naive_tuples) - len(oracle_tuples))
        report.append((n_workers, agree, len(oracle_tuples), len(naive_tuples), n_mismatched_tokens))
        print(f"  naive split, workers={n_workers}: matches oracle={agree}  "
              f"(oracle {len(oracle_tuples)} tokens vs naive {len(naive_tuples)} tokens, "
              f"~{n_mismatched_tokens} mismatched)")
    return report


def bench_scan_depth(dfa, skip, path):
    with open(path) as f:
        text = f.read()
    rows = []
    ns = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
    ns = [n for n in ns if n <= len(text)]
    print("\nScan-composition depth experiment (single process, no multiprocessing):")
    for n in ns:
        bounds = dl.naive_chunks(len(text), n)
        summaries = []
        last = len(bounds) - 1
        for i, (s, e) in enumerate(bounds):
            summaries.append(dl._phase1_worker((text[s:e], s, dfa, skip, i == last)))

        t0 = time.perf_counter()
        serial_result = dl.serial_scan(summaries, dfa)
        t1 = time.perf_counter()
        parallel_result, rounds = dl.parallel_scan(summaries, dfa)
        t2 = time.perf_counter()

        agree = all(
            (a.end == b.end and a.open == b.open) for a, b in zip(serial_result, parallel_result)
        )
        row = dict(
            n_chunks=n, serial_depth=n - 1 if n > 0 else 0, parallel_rounds=rounds,
            theoretical_log2=math.ceil(math.log2(n)) if n > 1 else 0,
            serial_time_s=t1 - t0, parallel_time_s=t2 - t1, results_agree=agree,
        )
        rows.append(row)
        print(f"  n_chunks={n:5d}  serial_depth={row['serial_depth']:5d}  "
              f"parallel_rounds={rounds:3d}  serial_time={row['serial_time_s']*1e6:8.1f} us  "
              f"parallel_time={row['parallel_time_s']*1e6:8.1f} us  agree={agree}")
    return rows


def main():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    print("Building lexer (regex -> NFA -> DFA -> minimized DFA)...")
    lexer = lexer_build.build_lexer()
    print(f"Raw DFA states: {lexer.raw_dfa.n_states}")
    print(f"Minimized DFA states: {lexer.min_dfa.n_states}")
    reduction_pct = 100.0 * (1 - lexer.min_dfa.n_states / lexer.raw_dfa.n_states)
    print(f"Reduction: {reduction_pct:.1f}%\n")

    print("Generating corpus...")
    corpus = sourcegen.write_corpus("corpus")
    for name, (path, size) in corpus.items():
        print(f"  {name}: {path} ({size} chars)")
    print()

    rows = []
    with Pool(16) as pool:
        for name, (path, _size) in corpus.items():
            bench_file(name, path, lexer.min_dfa, lexer.skip_tokens, pool, rows)
            print()

        print("Naive (state-unaware) splitting vs oracle, on the 'large' file:")
        naive_report = bench_naive_wrong(lexer.min_dfa, lexer.skip_tokens, pool, corpus["large"][0])
        print()

        scan_rows = bench_scan_depth(lexer.min_dfa, lexer.skip_tokens, corpus["large"][0])

    with open(f"{RESULTS_DIR}/results.json", "w") as f:
        json.dump(rows, f, indent=2)
    with open(f"{RESULTS_DIR}/results.csv", "w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    with open(f"{RESULTS_DIR}/scan_depth.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(scan_rows[0].keys()))
        writer.writeheader()
        writer.writerows(scan_rows)
    with open(f"{RESULTS_DIR}/dfa_sizes.json", "w") as f:
        json.dump(dict(raw_states=lexer.raw_dfa.n_states, min_states=lexer.min_dfa.n_states,
                        reduction_pct=reduction_pct), f, indent=2)
    with open(f"{RESULTS_DIR}/naive_wrong.json", "w") as f:
        json.dump(naive_report, f, indent=2)

    print(f"\nWrote results to {RESULTS_DIR}/")


if __name__ == "__main__":
    main()
