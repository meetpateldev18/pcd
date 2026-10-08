# Distributed Lexical Analysis via DFA Transition-Function Summaries

State-aware distributed lexical analysis: a hand-built regex -> NFA -> DFA ->
minimized-DFA pipeline (the classic `lex`/`flex` construction) feeding a
distributed tokenizer that splits a source file at arbitrary byte offsets
and uses parallel per-chunk state-transition summaries, composed through a
prefix scan, to recover the exact state each chunk really starts in before
tokenizing it for real.

No third-party dependencies -- everything here is the Python 3 standard
library (`dataclasses`, `multiprocessing`, `bisect`, `csv`, `json`).

## Files

| File | Purpose |
|---|---|
| `regex_engine.py` | Tiny regex parser (literals, classes, `\|`, `*`, `+`, `?`, groups) for writing token patterns |
| `automata.py` | Thompson construction (regex AST -> NFA), subset construction (NFA -> DFA), DFA minimization |
| `lexer_build.py` | Token grammar for the demo language; builds the combined raw and minimized DFA |
| `sequential_lexer.py` | Maximal-munch tokenizer on a DFA; the correctness oracle; the core primitive shared by distributed workers |
| `distributed_lexer.py` | Phase 1 (parallel per-chunk summaries), Phase 2 (serial fold vs. parallel prefix scan), Phase 3 (parallel final tokenization) |
| `sourcegen.py` | Synthetic source-file generator with deliberately boundary-crossing comments/strings |
| `tests.py` | Correctness tests, including an exhaustive single-cut-point regression test |
| `benchmark.py` | Runs the full experiment suite and writes `results/` |
| `report.md` | Write-up of the design, implementation, and measured results |

## How to run it

```bash
cd dla

# 1. Correctness tests (run this first -- fast, no corpus needed)
python3 tests.py

# 2. Regenerate the synthetic corpus (small/medium/large .src files)
python3 sourcegen.py corpus

# 3. Full benchmark: builds the lexer, generates the corpus, runs
#    sequential vs. distributed (both scan methods) across worker counts
#    1/2/4/8/16, the naive-splitting-is-wrong demo, and the scan-depth
#    experiment. Takes a few minutes on the "large" file.
python3 benchmark.py
```

`benchmark.py` writes its output to `results/`:
- `results.json` / `results.csv` -- one row per (file, worker count, scan method)
- `scan_depth.csv` -- serial-fold vs. parallel-scan depth/timing for chunk counts 1..1024
- `dfa_sizes.json` -- raw vs. minimized DFA state counts
- `naive_wrong.json` -- how badly naive (state-unaware) splitting disagrees with the oracle

To just play with the lexer interactively:

```python
import lexer_build, sequential_lexer as sl
lexer = lexer_build.build_lexer()
tokens = sl.tokenize("x = 1 + 2;", lexer.min_dfa, lexer.skip_tokens)
for t in tokens:
    print(t)
```

To re-run only the distributed engine on a specific file and worker count:

```python
from multiprocessing import Pool
import lexer_build, distributed_lexer as dl
lexer = lexer_build.build_lexer()
text = open("corpus/medium.src").read()
with Pool(8) as pool:
    result = dl.run_distributed(text, n_workers=8, dfa=lexer.min_dfa,
                                 skip_tokens=lexer.skip_tokens, pool=pool,
                                 scan_method="parallel")
print(result.timings)
```
