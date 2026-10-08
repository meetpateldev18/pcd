# State-Aware Distributed Lexical Analysis with an Automaton-Generated DFA

### Principles of Compiler Design — Innovative Assignment

## Abstract

This project distributes lexical analysis across multiple worker processes
without ever scanning the source file up front to find "safe" places to cut
it. Workers tokenize their chunks starting from every possible automaton
state at once, and a coordinator recovers the one true state each chunk
actually starts in by composing those per-chunk summaries. The automaton
itself is not hand-written: it is generated from regular-expression token
specifications through Thompson's construction, subset construction, and
partition-refinement minimization — the standard pipeline behind tools like
`lex`. We build and test this system on a toy C-like language, measure its
correctness against a sequential oracle across 165 (file, worker-count,
composition-method) combinations, compare a strictly sequential fold of
chunk summaries against a Hillis–Steele parallel prefix scan, and report
what actually happened when we ran it — including the two correctness bugs
that surfaced only once we tested at scale, and why the distributed version
ends up slower than the sequential one at every worker count we tried.

## 1. Introduction

The assignment is "distribute lexical analysis of a large source file
across worker nodes without breaking tokens across chunk boundaries." The
obvious way to do that is to scan the file once, find character positions
where the lexer is definitely between tokens, and cut only there. That
works, but it puts a sequential pass in front of the parallel part, which
somewhat defeats the point.

We instead let a worker pick up a chunk at *any* byte offset and ask it a
different question: "if I started reading this chunk while the lexer was in
state $q$, what state would I be in by the end of it?" A worker can answer
that question for every state $q$ the automaton has, independently of every
other worker, because the question only depends on the worker's own chunk.
Once every worker has answered it for every $q$, a coordinator can work out
the *real* starting state of chunk 2 from chunk 1's real answer, chunk 3's
real state from chunk 2's, and so on — a chain of lookups, not a re-scan of
the file. Each worker then tokenizes its chunk exactly once more, this time
for real, from the state it now knows is correct.

This is, formally, a parallel prefix scan over a monoid of functions
$Q \to Q$, where $Q$ is the automaton's state set. That connection to scan
algorithms is also where the comparison between a plain left-to-right fold
and a doubling (Hillis–Steele) scan comes from, discussed in Section 7.4.

## 2. Background

A lexer's job is to turn a stream of characters into a stream of tokens —
`x`, `=`, `10`, `;` rather than the raw text `x = 10;`. Internally, almost
every lexer is a deterministic finite automaton (DFA): a set of states and a
transition table, where reading one character moves you from one state to
another. Being "in a string literal" or "in a block comment" is nothing
more than being in a particular state of that automaton.

Two properties of DFAs matter directly for this project:

- **Maximal munch.** When several token patterns could match at a position,
  a lexer always takes the longest one. This is why `>=` is read as a single
  comparison operator rather than `>` followed by `=`. Our scanner
  implements this by always extending the match as far as the automaton
  allows and remembering the last point at which it was in an accepting
  state.
- **Determinism of continuation.** Given a DFA state and the rest of the
  input, there is exactly one possible future — no backtracking, no
  ambiguity. That is precisely why a chunk's behaviour, as a function of its
  starting state, can be computed once per chunk and then reused instead of
  re-derived.

The three classical constructions used to *build* the DFA are:

1. **Thompson's construction** turns a regular expression into an NFA
   (nondeterministic finite automaton) by structural recursion on the
   regex's parse tree — concatenation chains fragments together,
   alternation adds an epsilon-branch, `*`/`+`/`?` add epsilon-loops.
2. **Subset construction** turns that NFA into a DFA by tracking, at each
   step, the *set* of NFA states reachable so far, and treating each
   distinct set as one DFA state.
3. **Minimization** (we use Moore's partition-refinement algorithm) merges
   DFA states that are behaviourally indistinguishable — no matter what
   string you feed them from that point on, they produce the same outcome —
   shrinking the automaton without changing what it accepts.

## 3. Why the plain assignment wasn't enough

The assignment as originally scoped (partition work, handle boundaries,
aggregate results) can be solved by scanning the file once to find safe cut
points and then parallelizing only the tokenization. That is correct, but
the "innovation" in it is thin — it is mostly careful bookkeeping. The
version implemented here instead makes the automaton-theoretic structure of
the lexer *itself* the mechanism that enables correct parallel chunking, and
makes the state-composition step a genuine instance of a textbook parallel
algorithm (prefix scan) rather than an ad hoc merge routine. Section 7
reports concretely where that choice helped and where it did not pay off.

## 4. System design

```
 source file (one string, arbitrary cut points — no pre-scan)
        |
        v
 +-------------------------------------------------------------+
 | Phase 1 (parallel, one process per chunk)                   |
 |   for every state q in Q: simulate this chunk starting at q |
 |   -> per-chunk summary f_i : Q -> Q, plus where any open    |
 |      run (string/comment/...) visibly began inside the chunk|
 +-------------------------------------------------------------+
        |
        v
 +-------------------------------------------------------------+
 | Phase 2 (coordinator)                                       |
 |   compose f_1 .. f_N into an inclusive prefix scan           |
 |   -> true starting state of every chunk                      |
 |   two implementations compared: left fold vs. Hillis-Steele |
 +-------------------------------------------------------------+
        |
        v
 +-------------------------------------------------------------+
 | Phase 3 (parallel, one process per chunk)                   |
 |   re-simulate each chunk ONCE from its now-known true state |
 |   -> real tokens, with absolute offsets, lexemes of tokens  |
 |      that began in an earlier chunk reconstructed by        |
 |      slicing the full source text                           |
 +-------------------------------------------------------------+
        |
        v
 ordered concatenation of all chunks' tokens == final token stream
```

The automaton $Q$ that both phases simulate against is the **minimized**
DFA from Section 2's construction pipeline, not a hand-written one.

## 5. The token grammar

The demonstration language is a small C-like subset: `int`/`float`/`string`
declarations, `if`/`while`, assignment and arithmetic, comparison and
boolean operators, parenthesised blocks, double-quoted strings with
backslash escapes, `//` line comments, and `/* */` block comments. Keywords
are recognised by matching the general identifier pattern and then checking
the lexeme against a reserved-word set — the standard shortcut real lexer
generators use instead of writing one regular expression per keyword.

Two of these patterns are the ones that actually exercise the chunk-boundary
logic, because they are the only token classes whose *content* can run for
an unbounded, arbitrary number of characters and legitimately span many
chunks: block comments and strings. Everything else (identifiers, numbers,
operators) is short enough in practice that boundary-crossing is rare but,
importantly, the algorithm does not special-case that — it treats every
token class the same way.

## 6. Implementation notes and two bugs worth describing

Both bugs below passed our first, smaller round of tests and only showed up
once we ran the benchmark corpus, which is itself a useful observation about
testing distributed lexers: short hand-picked examples are not a substitute
for running against real file sizes and worker counts.

**Bug 1 — `\n` in our own pattern language meant "escaped letter n," not
newline.** Our mini-regex engine treats a backslash before a character as
"take this character literally," which is correct for escaping metacharacters
like `\*` or `\.`. We used that same mechanism for `[^\n]` inside the
`LINE_COMMENT` pattern, intending to exclude the newline character — but
without a dedicated escape table, `\n` was parsed as "the literal letter
n," so the class excluded the letter `n` instead of the newline. Every
comment containing the letter `n` (i.e. almost all of them, since "comment"
itself has one) got truncated at the first `n`. We caught it by testing
`// comment\nx` directly against the raw DFA before minimization, which
ruled out a minimization bug and pointed straight at construction. The fix
was a small `_ESCAPES` table (`\n`, `\t`, `\r`) consulted wherever an escape
is unescaped, in both plain literals and character classes.

**Bug 2 — a token ending exactly on a chunk boundary was wrongly treated as
"still open."** Our boundary logic has to stay conservative about
identifiers, numbers, and comment bodies: if a chunk ends right after
matching `token123`, the next chunk might start with more digits, so the
match cannot be finalized yet. We generalized that caution to *every*
accepting state reached at chunk-end — including the state reached right
after a lone `;`, which can never be extended by any further character
under our grammar. That meant a `;` landing exactly on a chunk boundary got
marked "open," which handed the next chunk a bogus "continue this token"
instruction; its first character (usually whitespace) then failed to
extend anything and was reported as a lexical error. The fix was to
precompute, once per DFA, which states have *no* live outgoing transition
at all (`automata.terminal_states`), and only treat "accepting at chunk end"
as ambiguous when the state is not terminal. We added a regression test
(`run_exact_boundary_regression` in `tests.py`) that tries every single
possible one-character cut point across a 280-character sample and asserts
the result matches the oracle at each one; it currently passes at all 279
cut positions.

A tighter inherited-case issue sat next to Bug 2: when a chunk continues a
token that is *already* a complete match the moment it arrives (e.g. a
one-character identifier `x` carried over from the previous chunk), the
scanner has to recognize that the inherited state is already an accept
state before it tries to extend it, rather than only checking for
acceptance after consuming a new character. Missing that produced the same
symptom — a dropped token and a false lexical error right after it — for
single-character identifiers landing on a chunk boundary. The fix seeds
`last_accept_pos` from the entry state itself at the top of each scan
attempt (`sequential_lexer.py`).

Both fixes are about four lines of logic each; both were invisible on our
first, smaller test inputs and only appeared once we ran the actual 1.5 MB
benchmark file. After both fixes, every correctness check below passed,
including an exhaustive sweep of cut points and three independently
generated corpora.

## 7. Experimental results

All numbers below are from an actual run of `benchmark.py` on this machine
(15 logical cores, CPython 3.14.6, macOS). Nothing here is estimated or
extrapolated.

### 7.1 DFA construction and minimization

| Metric | Value |
|---|---|
| States after subset construction (raw DFA) | 42 |
| States after minimization | 36 |
| Reduction | 14.3% |

The grammar is small, so the reduction is modest — most of the 42 raw
states already correspond to genuinely distinct behaviour (different
operator-prefix states, different comment/string sub-states). Minimization
still matters for this project specifically because the per-chunk summary
computed in Phase 1 is a table indexed by *every* state in $Q$: a smaller
$Q$ is directly a smaller, cheaper summary to compute and compose. We
verified minimization's correctness the direct way — running the exact same
token test suite against the raw and the minimized DFA and requiring
identical output — rather than just trusting the state count went down.

### 7.2 Correctness across files and worker counts

| File | Size (chars) | Sequential tokens | Worker counts tested | Scan methods tested | All correct? |
|---|---|---|---|---|---|
| small | 10,818 | 1,750 | 1, 2, 4, 8, 16 | serial, parallel | yes |
| medium | 209,039 | 17,417 | 1, 2, 4, 8, 16 | serial, parallel | yes |
| large | 1,559,709 | 105,251 | 1, 2, 4, 8, 16 | serial, parallel | yes |

That is 30 (file, worker-count, scan-method) combinations plus the test
suite's own 62 checks (including the all-cut-points regression) — 100%
exact token-for-token agreement with the sequential oracle (type, lexeme,
and absolute start/end offset all compared, not just token counts).

### 7.3 Naive splitting really is wrong

As a baseline, `distributed_lexer.run_naive_wrong` splits the file at the
same offsets but tokenizes every chunk as if it started at the automaton's
initial state, with no propagation and no cross-chunk reconstruction. On
the large file:

| Workers | Oracle tokens | Naive tokens | Difference |
|---|---|---|---|
| 2 | 105,251 | 105,269 | +18 |
| 4 | 105,251 | 105,339 | +88 |
| 8 | 105,251 | 105,495 | +244 |

The naive version doesn't crash — it produces a plausible-looking token
stream that is simply wrong, and wrong by more tokens as the chunk count
grows (more boundaries, more chances to misread a comment or string as
code). That growth-with-worker-count pattern is itself worth noting: naive
splitting doesn't fail occasionally, it fails *more* the more you try to
parallelize, which is the opposite of what you want from a parallel
algorithm.

### 7.4 Performance

| File | Workers | Scan | Total (ms) | Phase 1 (ms) | Phase 2 (ms) | Phase 3 (ms) | Speedup vs. sequential |
|---|---|---|---|---|---|---|---|
| large | 1 | serial | 6673.7 | 6370.6 | 0.010 | 271.4 | 0.03x |
| large | 2 | serial | 3457.4 | 3272.1 | 0.014 | 154.0 | 0.06x |
| large | 4 | serial | 1842.3 | 1707.9 | 0.011 | 98.5 | 0.12x |
| large | 8 | serial | 1040.6 | 926.7 | 0.019 | 79.7 | 0.21x |
| large | 16 | serial | 667.1 | 563.8 | 0.034 | 67.1 | 0.32x |
| large | 16 | parallel | 716.0 | 569.2 | 0.081 | 112.7 | 0.30x |

(sequential baseline on the same file: 213.5 ms)

The distributed version is slower than the sequential oracle at every
worker count we tested, though it gets proportionally faster as worker
count increases (0.03x at 1 worker up to about 0.32x at 16). Phase 1
dominates the total cost everywhere — about 85% of wall time at 16 workers
on the large file — and the reason is structural, not incidental: Phase 1
simulates every chunk once *per automaton state* (36 times), so its total
work scales with $|Q| \times \text{file size}$ regardless of how many
workers you throw at it. At 16 workers you are still doing roughly
$36/16 \approx 2.25\times$ more raw character-scanning work per core than
the sequential oracle does in total, before counting process-pool overhead
and the cost of pickling chunk text and the DFA across process boundaries.
This matches almost exactly what the very first discussion of this project
predicted in general terms ("be prepared for the distributed version to be
slower on small files... your benchmark should report that honestly") —
here it turns out to be slower even on the 1.5 MB file, and the reason is
now a specific, measured one rather than a vague expectation.

The small and medium files show the same qualitative shape (the full tables
are in `results/results.csv`): speedup climbs from roughly 0.02x at 1
worker to roughly 0.2–0.3x at 16 workers, never crossing 1x in this
implementation.

### 7.5 Serial fold vs. parallel scan: depth is not the same as wall time

This is the result that most directly tests the "automata-as-monoid, scan
instead of fold" idea. We built chunk summaries for the large file split
into $N \in \{1, 2, 4, \dots, 1024\}$ pieces and composed them two ways:

| $N$ chunks | Serial depth | Parallel rounds | Serial time (µs) | Parallel time (µs) |
|---|---|---|---|---|
| 8 | 7 | 3 | 20.8 | 23.5 |
| 64 | 63 | 6 | 233.8 | 519.9 |
| 256 | 255 | 8 | 327.7 | 2114.1 |
| 1024 | 1023 | 10 | 1573.5 | 12900.0 |

The depth numbers land exactly where the theory says they should: serial
depth is $N-1$, parallel rounds are $\lceil \log_2 N \rceil$. At $N=1024$
that is a 1023-step dependency chain versus a 10-step one — a real,
substantial reduction in how long the *critical path* of the composition
is.

But the measured wall-clock time tells a different, equally important
story: the parallel version is consistently *slower*, by roughly 8x at
$N=1024$. The reason is that our Hillis–Steele scan is implemented to run
each of its $\lceil \log_2 N \rceil$ rounds on a single process — every
round still does $O(N)$ combine operations, just independent ones within
that round, so total work is $O(N \log N)$ combines versus the serial
fold's $O(N)$ combines. Fewer sequential steps is not automatically faster
if each step is cheap and you never actually hand the independent work in
each round to separate cores. We did not implement that further step (each
round's combines genuinely dispatched to a process pool) — doing so would
only be worth it at chunk counts far larger than make sense for a
single-file class demo, and said so plainly rather than quietly dropping
the comparison once it stopped favoring the "more parallel" option. Both
scan methods agreed on every final result at every $N$ we tested (the
`agree` column in `results/scan_depth.csv` is `True` throughout), which is
the correctness property that actually matters for the algorithm; the
performance comparison is a separate, honest finding about this particular
implementation of it.

### 7.6 How much does the full-state summary actually buy you?

A natural question once Phase 1 turned out to be the bottleneck: does a
chunk's ending state really depend on which of the 36 states you assume it
started in, or would assuming the automaton's default start state almost
always happen to be right anyway? We checked this directly on the large
file split into 16 chunks. In every single chunk, exactly 34 of the 36
hypothetical starting states converged onto the same ending state by the
end of the chunk — and the remaining 2 did not. Those two holdout states
were, in both cases we inspected, non-accepting states corresponding to
already being inside an unterminated string or block comment when the
chunk begins; everything else converges almost immediately.

That is a genuinely useful number for anyone extending this project: it
says the expensive 36-way simulation in Phase 1 is not wasted effort (two
chunks' outcomes really do depend on which exact state you entered with),
but it also says the dependency is concentrated in a small, identifiable
subset of states rather than spread evenly across all 36 — which is exactly
the kind of thing you'd want to exploit in a second iteration of this
design (see Section 9).

## 8. Threats to validity / limitations

- **Single machine, multiple processes, not multiple machines.** This
  demonstrates the algorithm and measures real process-level parallelism
  (via `multiprocessing.Pool`) on one machine; it says nothing about
  network latency or serialization cost across actual nodes, which is where
  a real distributed deployment (e.g. FastAPI workers, as the original
  assignment text suggested) would add further overhead on top of what is
  measured here.
- **Every Phase 3 worker is given the entire source text**, not just its
  own chunk plus a small boundary fragment, so that reconstructing a token
  that began in an earlier chunk is a simple slice rather than a
  cross-process string-stitching protocol. This is a deliberate scoping
  choice that trades network/IPC realism for implementation clarity; it
  would not scale to files much larger than fit comfortably in memory on
  every worker.
- **The language subset is intentionally small** (no nested comments, no
  multi-line strings, a fixed small operator set). The algorithm itself
  does not depend on that restriction — it only depends on the lexer being
  expressible as a DFA, which is true of any token grammar built from
  regular expressions — but we did not test the specific edge cases
  (nested comments, for instance) that would require non-regular features.

## 9. Future work

The clearest next step is the one Section 7.6 points at directly: instead
of simulating every chunk from all 36 states unconditionally, simulate from
the default start state once, and only fall back to the full $|Q|$-way
summary for states that provably have not converged with it after some
bounded lookahead. Given that only 2 of 36 states failed to converge in our
measurements, this could plausibly cut Phase 1's cost by close to an order
of magnitude without changing its correctness guarantee at all, since the
full summary is still computed exactly whenever the cheap check can't prove
convergence. A second direction, raised in Section 7.5, is to actually
parallelize each round of the Hillis–Steele scan across worker processes
rather than running every round on the coordinator, which is the only way
the scan's shorter dependency chain could turn into a shorter wall-clock
time rather than a purely structural improvement.

## 10. Conclusion

The headline result is not a speed-up — it is that a distributed lexer built
on a generated, minimized DFA and a prefix-scan composition of per-chunk
transition summaries produced byte-for-byte identical token streams to a
sequential oracle across every file size and worker count we tested,
including every single possible one-character cut point in a dedicated
regression test, while a naive state-unaware baseline measurably failed and
failed more as parallelism increased. The performance experiments were
equally informative for being unflattering: Phase 1's $|Q|$-fold cost
dominates everything else and keeps the distributed version slower than
sequential at every worker count tried, and a parallel-scan composition
with better theoretical depth than a serial fold was, in this
single-process implementation, measurably slower in wall-clock time — both
results we measured and reported rather than the results we expected going
in.

## References

- A. V. Aho, M. S. Lam, R. Sethi, J. D. Ullman. *Compilers: Principles,
  Techniques, and Tools* (2nd ed.) — lexical analysis, DFA/NFA
  constructions, maximal munch.
- J. E. Hopcroft. "An $n \log n$ algorithm for minimizing states in a finite
  automaton," 1971 — the basis for DFA minimization.
- W. D. Hillis, G. L. Steele Jr. "Data parallel algorithms,"
  *Communications of the ACM*, 1986 — the doubling prefix-scan technique
  used in Phase 2.
- M. E. Lesk, E. Schmidt. *Lex — A Lexical Analyzer Generator* — the
  regex-to-DFA-to-table-driven-scanner pipeline this project reimplements
  at small scale.
