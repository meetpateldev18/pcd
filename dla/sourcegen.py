"""
Synthetic source-file generator for the toy language the lexer recognizes.
Deliberately injects large block comments and strings so that, at various
worker counts, chunk boundaries are guaranteed to fall inside them -- that is
exactly the case the whole project exists to handle correctly.
"""

import random

IDENT_POOL = [f"var{i}" for i in range(40)] + ["count", "total", "flag", "index", "result"]
KEYWORDS_FOR_DECL = ["int", "float", "string"]


def _filler_text(rng: random.Random, length: int) -> str:
    words = ["lorem", "ipsum", "token", "state", "automaton", "parse", "chunk",
             "offset", "scan", "dfa", "nfa", "thompson", "subset", "minimize"]
    out = []
    n = 0
    while n < length:
        w = rng.choice(words)
        out.append(w)
        n += len(w) + 1
    return " ".join(out)[:length]


def _gen_statement(rng: random.Random) -> str:
    kind = rng.choice(["decl", "assign", "if", "while", "expr"])
    name = rng.choice(IDENT_POOL)
    if kind == "decl":
        t = rng.choice(KEYWORDS_FOR_DECL)
        if t == "string":
            val = f'"{_filler_text(rng, rng.randint(3, 15))}"'
        elif t == "float":
            val = f"{rng.randint(0, 99)}.{rng.randint(0, 99)}"
        else:
            val = str(rng.randint(0, 9999))
        return f"{t} {name} = {val};"
    if kind == "assign":
        op = rng.choice(["+", "-", "*"])
        return f"{name} = {name} {op} {rng.randint(1, 100)};"
    if kind == "if":
        cmp = rng.choice(["==", "!=", "<=", ">=", "<", ">"])
        return f"if ({name} {cmp} {rng.randint(0, 100)}) {{ {name} = 0; }}"
    if kind == "while":
        return f"while ({name} < {rng.randint(1, 50)}) {{ {name} = {name} + 1; }}"
    return f"{name} + {rng.randint(1, 10)} * {rng.randint(1, 10)};"


def generate_source(n_statements: int, seed: int = 0,
                     big_comment_every: int = 40,
                     big_comment_len: int = 4000) -> str:
    rng = random.Random(seed)
    lines = []
    for i in range(n_statements):
        lines.append(_gen_statement(rng))
        if rng.random() < 0.15:
            lines.append(f"// {_filler_text(rng, rng.randint(5, 40))}")
        if big_comment_every and i % big_comment_every == big_comment_every - 1:
            body = _filler_text(rng, big_comment_len)
            # deliberately NOT aligned to any particular boundary -- a long
            # run like this will straddle chunk cuts at most worker counts
            lines.append(f"/* {body} */")
    return "\n".join(lines) + "\n"


def write_corpus(out_dir: str):
    import os
    os.makedirs(out_dir, exist_ok=True)
    sizes = {
        "small": dict(n_statements=200, big_comment_every=30, big_comment_len=800),
        "medium": dict(n_statements=2000, big_comment_every=40, big_comment_len=3000),
        "large": dict(n_statements=12000, big_comment_every=60, big_comment_len=6000),
    }
    paths = {}
    for name, kwargs in sizes.items():
        text = generate_source(seed=hash(name) & 0xFFFF, **kwargs)
        path = f"{out_dir}/{name}.src"
        with open(path, "w") as f:
            f.write(text)
        paths[name] = (path, len(text))
    return paths


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "corpus"
    for name, (path, size) in write_corpus(out).items():
        print(f"{name}: {path} ({size} chars)")
