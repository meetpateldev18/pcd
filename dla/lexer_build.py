"""
Token grammar for the demonstration language subset, and the pipeline that
turns it into a single combined lexer DFA:

    regex specs --(Thompson construction)--> NFA
               --(subset construction)-->   DFA
               --(partition refinement)-->  minimized DFA

Both the raw and the minimized DFA are kept around because comparing their
state counts (and the cost of the per-chunk transition-function summaries
built from each) is itself one of the experiments in this project.
"""

from dataclasses import dataclass
from typing import Optional

import automata
import regex_engine as rx

METACHARS = set("|*+?()[]^.\\")


def lit(literal: str) -> str:
    """Escape a literal string so it can be used as a regex pattern."""
    return "".join(("\\" + c) if c in METACHARS else c for c in literal)


KEYWORDS = {
    "if", "else", "while", "for", "return", "int", "float", "string",
    "void", "true", "false", "break", "continue",
}

# (name, pattern, priority). Lower priority number wins ties at equal match
# length; longest match always wins regardless of priority (maximal munch).
TOKEN_SPECS = [
    ("WHITESPACE", r"[ \t\n]+", 0),
    ("BLOCK_COMMENT", r"/\*([^*]|\*+[^*/])*\*+/", 1),
    ("LINE_COMMENT", r"//[^\n]*", 2),
    ("STRING", r'"([^"\\]|\\.)*"', 3),
    ("NUMBER", r"[0-9]+(\.[0-9]+)?", 4),
    ("IDENT", r"[A-Za-z_][A-Za-z_0-9]*", 5),
    ("OP_EQ", lit("=="), 6),
    ("OP_NE", lit("!="), 6),
    ("OP_LE", lit("<="), 6),
    ("OP_GE", lit(">="), 6),
    ("OP_AND", lit("&&"), 6),
    ("OP_OR", lit("||"), 6),
    ("ASSIGN", lit("="), 7),
    ("LT", lit("<"), 7),
    ("GT", lit(">"), 7),
    ("NOT", lit("!"), 7),
    ("PLUS", lit("+"), 7),
    ("MINUS", lit("-"), 7),
    ("STAR", lit("*"), 7),
    ("SLASH", lit("/"), 7),
    ("LPAREN", lit("("), 7),
    ("RPAREN", lit(")"), 7),
    ("LBRACE", lit("{"), 7),
    ("RBRACE", lit("}"), 7),
    ("SEMI", lit(";"), 7),
    ("COMMA", lit(","), 7),
]

SKIP_TOKENS = {"WHITESPACE"}


@dataclass
class CompiledLexer:
    raw_dfa: automata.DFA
    min_dfa: automata.DFA
    skip_tokens: frozenset


def build_lexer() -> CompiledLexer:
    specs = [(name, rx.parse(pattern), priority) for name, pattern, priority in TOKEN_SPECS]
    nfa = automata.combine_patterns(specs)
    raw_dfa = automata.subset_construction(nfa, rx.ALPHABET)
    min_dfa = automata.minimize(raw_dfa)
    raw_dfa.terminal = automata.terminal_states(raw_dfa)
    min_dfa.terminal = automata.terminal_states(min_dfa)
    return CompiledLexer(raw_dfa=raw_dfa, min_dfa=min_dfa, skip_tokens=frozenset(SKIP_TOKENS))


def classify_lexeme(token_name: str, lexeme: str) -> str:
    """Post-pass: an IDENT that happens to be a reserved word becomes KEYWORD.
    This is the standard practical shortcut lex implementations use instead of
    writing one regex per keyword."""
    if token_name == "IDENT" and lexeme in KEYWORDS:
        return "KEYWORD"
    return token_name
