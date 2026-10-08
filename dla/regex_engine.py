"""
Tiny regular-expression engine used only to DEFINE token patterns.

Supports: literals, escapes (\\x), character classes ([abc], [a-z], [^...]),
grouping (...), alternation (a|b), concatenation, and the postfix repetition
operators * + ?. "." means "any symbol in ALPHABET".

This is deliberately small -- just enough to express the token grammar for
the distributed lexer project (identifiers, numbers, strings, comments,
operators). It is not meant to be a general-purpose regex library.
"""

from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple, Union

# Working alphabet for the whole lexer. Anything outside this set that shows
# up in source text is treated as a lexical error by the DFA simulation.
ALPHABET: FrozenSet[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
    " \t\n"
    "+-*/=<>!&|(){};,.\"'\\_:"
)

_METACHARS = set("|*+?()[]^.\\")

# Standard escape sequences, e.g. \n in a pattern means "newline character",
# not "escaped letter n". Anything not listed here just escapes itself
# (used to put literal metacharacters like \* or \. into a pattern).
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _unescape(c: str) -> str:
    return _ESCAPES.get(c, c)


# ---------------------------------------------------------------- AST nodes

class Node:
    pass


@dataclass
class Lit(Node):
    ch: str


@dataclass
class AnyChar(Node):
    pass


@dataclass
class Cls(Node):
    chars: FrozenSet[str]
    negate: bool

    def resolved(self) -> FrozenSet[str]:
        return (ALPHABET - self.chars) if self.negate else self.chars


@dataclass
class Concat(Node):
    parts: List[Node]


@dataclass
class Alt(Node):
    options: List[Node]


@dataclass
class Star(Node):
    inner: Node


@dataclass
class Plus(Node):
    inner: Node


@dataclass
class Opt(Node):
    inner: Node


# -------------------------------------------------------------- the parser

class RegexSyntaxError(ValueError):
    pass


class _Parser:
    def __init__(self, pattern: str):
        self.s = pattern
        self.i = 0
        self.n = len(pattern)

    def peek(self) -> Optional[str]:
        return self.s[self.i] if self.i < self.n else None

    def advance(self) -> str:
        c = self.s[self.i]
        self.i += 1
        return c

    def parse(self) -> Node:
        node = self.parse_alt()
        if self.i != self.n:
            raise RegexSyntaxError(f"unexpected '{self.peek()}' at {self.i} in {self.s!r}")
        return node

    def parse_alt(self) -> Node:
        options = [self.parse_concat()]
        while self.peek() == "|":
            self.advance()
            options.append(self.parse_concat())
        return options[0] if len(options) == 1 else Alt(options)

    def parse_concat(self) -> Node:
        parts = []
        while self.peek() is not None and self.peek() not in "|)":
            parts.append(self.parse_repeat())
        if not parts:
            return Concat([])
        return parts[0] if len(parts) == 1 else Concat(parts)

    def parse_repeat(self) -> Node:
        atom = self.parse_atom()
        while self.peek() in ("*", "+", "?"):
            op = self.advance()
            atom = {"*": Star, "+": Plus, "?": Opt}[op](atom)
        return atom

    def parse_atom(self) -> Node:
        c = self.peek()
        if c is None:
            raise RegexSyntaxError("unexpected end of pattern")
        if c == "(":
            self.advance()
            node = self.parse_alt()
            if self.peek() != ")":
                raise RegexSyntaxError("missing closing ')'")
            self.advance()
            return node
        if c == "[":
            return self.parse_class()
        if c == ".":
            self.advance()
            return AnyChar()
        if c == "\\":
            self.advance()
            if self.peek() is None:
                raise RegexSyntaxError("dangling escape")
            return Lit(_unescape(self.advance()))
        if c in _METACHARS:
            raise RegexSyntaxError(f"unescaped metachar '{c}' at {self.i}")
        self.advance()
        return Lit(c)

    def parse_class(self) -> Node:
        self.advance()  # consume '['
        negate = False
        if self.peek() == "^":
            negate = True
            self.advance()
        chars = set()
        first = True
        while self.peek() is not None and (self.peek() != "]" or first):
            first = False
            c = self.advance()
            if c == "\\":
                if self.peek() is None:
                    raise RegexSyntaxError("dangling escape in class")
                c = _unescape(self.advance())
            # range a-z
            if self.peek() == "-" and self.i + 1 < self.n and self.s[self.i + 1] != "]":
                self.advance()  # consume '-'
                end = self.advance()
                if end == "\\":
                    end = _unescape(self.advance())
                chars.update(chr(o) for o in range(ord(c), ord(end) + 1))
            else:
                chars.add(c)
        if self.peek() != "]":
            raise RegexSyntaxError("missing closing ']'")
        self.advance()
        return Cls(frozenset(chars), negate)


def parse(pattern: str) -> Node:
    return _Parser(pattern).parse()
