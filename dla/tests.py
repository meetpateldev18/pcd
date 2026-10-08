"""
Correctness tests. Run with: python3 tests.py
No test framework dependency on purpose -- keep it runnable anywhere.
"""

import sys
from multiprocessing import Pool

import distributed_lexer as dl
import lexer_build
import sequential_lexer as sl
import sourcegen

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append((name, detail))
        print(f"FAIL: {name}  {detail}")


def tok_tuple(t):
    return (t.type, t.lexeme, t.start, t.end)


def run_minimization_tests(lexer):
    check("minimization does not increase state count",
          lexer.min_dfa.n_states <= lexer.raw_dfa.n_states,
          f"raw={lexer.raw_dfa.n_states} min={lexer.min_dfa.n_states}")
    check("minimization actually reduces states for this grammar",
          lexer.min_dfa.n_states < lexer.raw_dfa.n_states,
          f"raw={lexer.raw_dfa.n_states} min={lexer.min_dfa.n_states}")


def run_token_tests(dfa, skip):
    cases = [
        ("x = 10;", [("IDENT", "x"), ("ASSIGN", "="), ("NUMBER", "10"), ("SEMI", ";")]),
        ("x>=10", [("IDENT", "x"), ("OP_GE", ">="), ("NUMBER", "10")]),
        ("x>=10 vs x > = 10",
         [("IDENT", "x"), ("OP_GE", ">="), ("NUMBER", "10"), ("IDENT", "vs"),
          ("IDENT", "x"), ("GT", ">"), ("ASSIGN", "="), ("NUMBER", "10")]),
        ("if (x) {}", [("KEYWORD", "if"), ("LPAREN", "("), ("IDENT", "x"),
                        ("RPAREN", ")"), ("LBRACE", "{"), ("RBRACE", "}")]),
        ('"a\\"b"', [("STRING", '"a\\"b"')]),
        ("3.14 7", [("NUMBER", "3.14"), ("NUMBER", "7")]),
        ("// comment\nx", [("LINE_COMMENT", "// comment"), ("IDENT", "x")]),
        ("/* a * b */x", [("BLOCK_COMMENT", "/* a * b */"), ("IDENT", "x")]),
        ("/* a /* nested-ish */ x", [("BLOCK_COMMENT", "/* a /* nested-ish */"), ("IDENT", "x")]),
        ("@", [("ERROR", "@")]),
    ]
    for text, expected in cases:
        toks = sl.tokenize(text, dfa, skip)
        got = [(t.type, t.lexeme) for t in toks]
        check(f"maximal-munch/basic: {text!r}", got == expected, f"got={got}")


def run_boundary_tests(lexer, pool):
    dfa, skip = lexer.min_dfa, lexer.skip_tokens
    samples = [
        "x = 1; /* " + ("filler " * 200) + " */ y = 2;",
        '"' + ("abc " * 100) + '"; x = 1;',
        "x = 1;\n" * 5 + "/* " + ("c " * 500) + " */\n" + "y = 2;\n" * 5,
    ]
    for text in samples:
        oracle = [tok_tuple(t) for t in sl.tokenize(text, dfa, skip)]
        for n_workers in (1, 2, 3, 4, 8):
            if n_workers > len(text):
                continue
            for method in ("serial", "parallel"):
                result = dl.run_distributed(text, n_workers, dfa, skip, pool, scan_method=method)
                got = [tok_tuple(t) for t in result.tokens]
                check(f"boundary n={n_workers} method={method} len={len(text)}",
                      got == oracle,
                      f"oracle_len={len(oracle)} got_len={len(got)} "
                      f"first_diff={next(((i, oracle[i], got[i] if i < len(got) else None) for i in range(max(len(oracle), len(got))) if i >= len(oracle) or i >= len(got) or oracle[i] != got[i]), None)}")


def run_exact_boundary_regression(lexer, pool):
    """Regression test: a fixed-length token (e.g. ';') landing exactly on a
    chunk boundary must not be mistaken for an 'open run'. This caught a real
    bug where the next chunk's first character was misread as a lexical
    error because the ';' was never closed out."""
    dfa, skip = lexer.min_dfa, lexer.skip_tokens
    text = "x = 1;\ny = 2;\n" * 20
    oracle = [tok_tuple(t) for t in sl.tokenize(text, dfa, skip)]
    for cut in range(1, len(text)):
        bounds = [(0, cut), (cut, len(text))]
        with_fixed_bounds = dl.phase1_build_summaries(text, bounds, dfa, skip, pool)
        scan_result, _ = dl.parallel_scan(with_fixed_bounds, dfa)
        true_start, true_open, final_state = dl.true_states_from_scan(scan_result, dfa)
        tokens = dl.phase3_tokenize(text, bounds, dfa, skip, true_start, true_open, pool)
        if final_state != dfa.start:
            tokens.append(sl.Token("ERROR_UNTERMINATED", "", len(text), len(text)))
        got = [tok_tuple(t) for t in tokens]
        if got != oracle:
            check(f"exact-boundary cut at {cut}", False, "mismatch vs oracle")
            return
    check("exact-boundary regression (all cut points)", True)


def run_naive_is_wrong_demo(lexer, pool):
    dfa, skip = lexer.min_dfa, lexer.skip_tokens
    text = "x = 1; /* " + ("filler " * 300) + " */ y = 2;"
    oracle = [tok_tuple(t) for t in sl.tokenize(text, dfa, skip)]
    naive = [tok_tuple(t) for t in dl.run_naive_wrong(text, 4, dfa, skip, pool)]
    check("naive splitting SHOULD disagree with oracle on a boundary-crossing comment",
          naive != oracle, "naive splitting unexpectedly matched -- strengthen the test input")


def run_corpus_tests(lexer, pool):
    dfa, skip = lexer.min_dfa, lexer.skip_tokens
    text = sourcegen.generate_source(n_statements=150, seed=1, big_comment_every=20, big_comment_len=500)
    oracle = [tok_tuple(t) for t in sl.tokenize(text, dfa, skip)]
    for n_workers in (1, 2, 4, 8):
        result = dl.run_distributed(text, n_workers, dfa, skip, pool, scan_method="parallel")
        got = [tok_tuple(t) for t in result.tokens]
        check(f"generated corpus n={n_workers}", got == oracle, f"lens oracle={len(oracle)} got={len(got)}")
        result2 = dl.run_distributed(text, n_workers, dfa, skip, pool, scan_method="serial")
        got2 = [tok_tuple(t) for t in result2.tokens]
        check(f"generated corpus (serial scan) n={n_workers}", got2 == oracle)


def main():
    lexer = lexer_build.build_lexer()
    run_minimization_tests(lexer)
    run_token_tests(lexer.min_dfa, lexer.skip_tokens)
    run_token_tests(lexer.raw_dfa, lexer.skip_tokens)  # raw and minimized must agree
    with Pool(4) as pool:
        run_boundary_tests(lexer, pool)
        run_exact_boundary_regression(lexer, pool)
        run_naive_is_wrong_demo(lexer, pool)
        run_corpus_tests(lexer, pool)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
