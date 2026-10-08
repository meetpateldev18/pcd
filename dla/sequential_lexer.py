"""
Maximal-munch tokenizer built directly on a compiled DFA. `tokenize()` is the
whole-file entry point and doubles as the correctness oracle for the
distributed engine. `tokenize_core()` is the lower-level primitive shared
with the distributed workers: it can start mid-automaton (continuing a token
that began in an earlier chunk) and can stop mid-automaton (when a chunk ends
inside an unfinished token/comment/string), reporting enough information for
the caller to stitch chunks back together correctly.
"""

import bisect
from dataclasses import dataclass
from typing import List, Optional

import automata
from lexer_build import classify_lexeme


@dataclass
class Token:
    type: str
    lexeme: str
    start: int
    end: int
    line: int = 0
    col: int = 0


def step(dfa: automata.DFA, state: int, sym: str) -> int:
    return dfa.table[state].get(sym, dfa.dead)


def tokenize_core(text: str, dfa: automata.DFA, skip_tokens: frozenset, *,
                   start_offset: int = 0,
                   start_state: Optional[int] = None,
                   inherited_open_start: Optional[int] = None,
                   full_source: Optional[str] = None,
                   at_eof: bool = True):
    """Scan `text` (a slice of `full_source` beginning at `start_offset`).

    `at_eof` says whether `text` reaches the true end of the whole source.
    This matters for unbounded-length tokens (identifiers, numbers, comment
    bodies, ...): if a chunk ends exactly on an accepting state but more text
    could still follow (at_eof=False), the match is NOT final -- the next
    chunk might extend it (maximal munch has to see the next character to
    know it should stop). At the true end of file there is no "might extend"
    case, so an accepting state at the last position IS the final answer.

    Returns (tokens, end_state, local_open_start):
      - end_state: the DFA state the scan finishes in. Equal to dfa.start iff
        there is no token/comment/string left open at the end of `text`.
      - local_open_start: absolute offset (in full_source) where a run still
        open at the end of `text` began, if that start is visible within
        `text` itself; None if either there's no open run, or the open run
        predates `text` (started before `start_offset` -- the caller already
        knows that via `inherited_open_start` and must keep propagating it).
    """
    tokens: List[Token] = []
    n = len(text)
    pos = 0
    entry_state = dfa.start if start_state is None else start_state
    state = entry_state

    while pos < n:
        cur = state
        i = pos
        # An inherited continuation can start on a state that is *already*
        # accepting (e.g. the single-character identifier "x" looked open
        # only because more letters might have followed). Seed the
        # best-match-so-far from that state before trying to extend it.
        if dfa.accept[cur] is not None:
            last_accept_pos, last_accept_name = i, dfa.accept[cur]
        else:
            last_accept_pos, last_accept_name = -1, None
        hit_dead = False
        while i < n:
            nxt = step(dfa, cur, text[i])
            if nxt == dfa.dead:
                hit_dead = True
                break
            cur = nxt
            i += 1
            if dfa.accept[cur] is not None:
                last_accept_pos = i
                last_accept_name = dfa.accept[cur]

        if i == n:
            # A match ending exactly at an accepting state right as the text
            # runs out is final -- not ambiguous -- if either we are truly
            # at EOF, or `cur` is a terminal state (no symbol can extend it
            # further, e.g. right after a lone ';'). Otherwise it might grow
            # once the next chunk's characters are appended.
            closed = last_accept_pos == i and (at_eof or cur in dfa.terminal)
            if not closed:
                if pos == 0 and entry_state != dfa.start:
                    return tokens, cur, None  # run predates this text entirely
                return tokens, cur, start_offset + pos

        if last_accept_pos == -1:
            # no valid token could even start here -- genuine lexical error
            tokens.append(Token("ERROR", text[pos:pos + 1],
                                 start_offset + pos, start_offset + pos + 1))
            pos += 1
            state = dfa.start
            entry_state = dfa.start
            continue

        name = last_accept_name
        end_pos = last_accept_pos
        if pos == 0 and entry_state != dfa.start:
            # this token began before `text` -- reconstruct its true span
            abs_start = inherited_open_start
            abs_end = start_offset + end_pos
            lexeme = (full_source[abs_start:abs_end] if full_source is not None
                       else text[pos:end_pos])
        else:
            abs_start = start_offset + pos
            abs_end = start_offset + end_pos
            lexeme = text[pos:end_pos]

        ttype = classify_lexeme(name, lexeme)
        if ttype not in skip_tokens:
            tokens.append(Token(ttype, lexeme, abs_start, abs_end))
        pos = end_pos
        state = dfa.start
        entry_state = dfa.start

    return tokens, state, None


def tokenize(text: str, dfa: automata.DFA, skip_tokens: frozenset) -> List[Token]:
    tokens, end_state, _ = tokenize_core(text, dfa, skip_tokens, full_source=text)
    if end_state != dfa.start:
        tokens.append(Token("ERROR_UNTERMINATED", "", len(text), len(text)))
    attach_positions(text, tokens)
    return tokens


def attach_positions(text: str, tokens: List[Token]) -> None:
    newline_offsets = [i for i, c in enumerate(text) if c == "\n"]
    for tok in tokens:
        line = bisect.bisect_right(newline_offsets, tok.start)
        col = tok.start - (newline_offsets[line - 1] + 1 if line > 0 else 0)
        tok.line = line + 1
        tok.col = col + 1
