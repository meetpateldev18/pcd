"""
Thompson construction (regex AST -> NFA), subset construction (NFA -> DFA),
and DFA minimization by partition refinement (Moore's algorithm).

This is the classic PCD "how lex/flex actually works" pipeline, applied to
several token patterns at once so the resulting machine is a single
multi-token lexer DFA rather than one DFA per token.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple

from regex_engine import ALPHABET, AnyChar, Alt, Cls, Concat, Lit, Node, Opt, Plus, Star

EPS = None  # sentinel used only inside NFA construction, never in DFA tables


# ------------------------------------------------------------------- NFA

@dataclass
class NFA:
    n_states: int = 0
    # trans[state][symbol] -> set of next states (symbol is a single char)
    trans: Dict[int, Dict[str, set]] = field(default_factory=dict)
    eps: Dict[int, set] = field(default_factory=dict)
    start: int = 0
    # accept[state] = (token_name, priority)  -- lower priority number wins ties
    accept: Dict[int, Tuple[str, int]] = field(default_factory=dict)

    def new_state(self) -> int:
        s = self.n_states
        self.n_states += 1
        self.trans[s] = {}
        self.eps[s] = set()
        return s

    def add_eps(self, a: int, b: int):
        self.eps[a].add(b)

    def add_sym(self, a: int, sym: str, b: int):
        self.trans[a].setdefault(sym, set()).add(b)


def _build(nfa: NFA, node: Node) -> Tuple[int, int]:
    """Returns (start, accept) states for the fragment representing `node`."""
    if isinstance(node, Lit):
        s, a = nfa.new_state(), nfa.new_state()
        nfa.add_sym(s, node.ch, a)
        return s, a
    if isinstance(node, AnyChar):
        s, a = nfa.new_state(), nfa.new_state()
        for ch in ALPHABET:
            nfa.add_sym(s, ch, a)
        return s, a
    if isinstance(node, Cls):
        s, a = nfa.new_state(), nfa.new_state()
        for ch in node.resolved():
            nfa.add_sym(s, ch, a)
        return s, a
    if isinstance(node, Concat):
        if not node.parts:
            s = nfa.new_state()
            return s, s
        start, acc = _build(nfa, node.parts[0])
        for part in node.parts[1:]:
            s2, a2 = _build(nfa, part)
            nfa.add_eps(acc, s2)
            acc = a2
        return start, acc
    if isinstance(node, Alt):
        s, a = nfa.new_state(), nfa.new_state()
        for opt in node.options:
            os, oa = _build(nfa, opt)
            nfa.add_eps(s, os)
            nfa.add_eps(oa, a)
        return s, a
    if isinstance(node, Star):
        s, a = nfa.new_state(), nfa.new_state()
        is_, ia = _build(nfa, node.inner)
        nfa.add_eps(s, is_)
        nfa.add_eps(s, a)
        nfa.add_eps(ia, is_)
        nfa.add_eps(ia, a)
        return s, a
    if isinstance(node, Plus):
        is_, ia = _build(nfa, node.inner)
        a = nfa.new_state()
        nfa.add_eps(ia, is_)
        nfa.add_eps(ia, a)
        return is_, a
    if isinstance(node, Opt):
        s, a = nfa.new_state(), nfa.new_state()
        is_, ia = _build(nfa, node.inner)
        nfa.add_eps(s, is_)
        nfa.add_eps(s, a)
        nfa.add_eps(ia, a)
        return s, a
    raise TypeError(f"unknown node type {type(node)}")


def thompson(node: Node) -> Tuple[NFA, int, int]:
    nfa = NFA()
    s, a = _build(nfa, node)
    return nfa, s, a


def combine_patterns(specs: List[Tuple[str, Node, int]]) -> NFA:
    """specs: list of (token_name, ast, priority). priority: lower = wins ties."""
    nfa = NFA()
    start = nfa.new_state()
    for name, ast, priority in specs:
        s, a = _build(nfa, ast)
        nfa.add_eps(start, s)
        nfa.accept[a] = (name, priority)
    nfa.start = start
    return nfa


# ------------------------------------------------------------------- DFA

@dataclass
class DFA:
    n_states: int
    start: int
    dead: int
    # table[state][symbol] -> state   (dense, symbol is a single char)
    table: Dict[int, Dict[str, int]]
    # accept[state] -> token_name or None
    accept: Dict[int, Optional[str]]
    alphabet: Tuple[str, ...]


def _eps_closure(nfa: NFA, states: FrozenSet[int]) -> FrozenSet[int]:
    stack = list(states)
    seen = set(states)
    while stack:
        s = stack.pop()
        for t in nfa.eps.get(s, ()):
            if t not in seen:
                seen.add(t)
                stack.append(t)
    return frozenset(seen)


def subset_construction(nfa: NFA, alphabet: FrozenSet[str]) -> DFA:
    alphabet = tuple(sorted(alphabet))
    start_set = _eps_closure(nfa, frozenset({nfa.start}))

    set_to_id: Dict[FrozenSet[int], int] = {}
    table: Dict[int, Dict[str, int]] = {}
    accept: Dict[int, Optional[str]] = {}

    def get_id(nfa_set: FrozenSet[int]) -> int:
        if nfa_set in set_to_id:
            return set_to_id[nfa_set]
        sid = len(set_to_id)
        set_to_id[nfa_set] = sid
        # which token (if any) does this DFA state accept, resolving ties by priority
        best = None
        for ns in nfa_set:
            if ns in nfa.accept:
                name, pr = nfa.accept[ns]
                if best is None or pr < best[1]:
                    best = (name, pr)
        accept[sid] = best[0] if best else None
        return sid

    dead_set = frozenset()
    dead_id = get_id(dead_set)
    start_id = get_id(start_set)

    queue = [start_set]
    visited = {start_set, dead_set}
    while queue:
        cur = queue.pop()
        cur_id = set_to_id[cur]
        table[cur_id] = {}
        for sym in alphabet:
            nxt = set()
            for ns in cur:
                nxt |= nfa.trans.get(ns, {}).get(sym, set())
            nxt_closed = _eps_closure(nfa, frozenset(nxt)) if nxt else dead_set
            nid = get_id(nxt_closed)
            table[cur_id][sym] = nid
            if nxt_closed not in visited:
                visited.add(nxt_closed)
                queue.append(nxt_closed)
    table.setdefault(dead_id, {sym: dead_id for sym in alphabet})

    return DFA(
        n_states=len(set_to_id),
        start=start_id,
        dead=dead_id,
        table=table,
        accept=accept,
        alphabet=alphabet,
    )


def minimize(dfa: DFA) -> DFA:
    """Partition refinement (Moore's algorithm). Keeps accepting states with
    different token types in separate classes (otherwise we'd merge e.g. an
    accepting NUMBER state with an accepting IDENT state just because their
    *future* behaviour happens to coincide)."""
    states = list(range(dfa.n_states))

    def signature(s: int) -> Tuple:
        return (dfa.accept[s],)

    # initial partition by accept-signature
    groups: Dict[Tuple, int] = {}
    partition = {}
    for s in states:
        sig = signature(s)
        if sig not in groups:
            groups[sig] = len(groups)
        partition[s] = groups[sig]

    changed = True
    while changed:
        changed = False
        new_groups: Dict[Tuple, int] = {}
        new_partition = {}
        for s in states:
            key = (partition[s], tuple(partition[dfa.table[s][sym]] for sym in dfa.alphabet))
            if key not in new_groups:
                new_groups[key] = len(new_groups)
            new_partition[s] = new_groups[key]
        if new_partition != partition:
            changed = True
        partition = new_partition

    # build minimized DFA using one representative state id per group
    n_min = len(set(partition.values()))
    rep_of_group: Dict[int, int] = {}
    for s in states:
        rep_of_group.setdefault(partition[s], s)

    new_table: Dict[int, Dict[str, int]] = {}
    new_accept: Dict[int, Optional[str]] = {}
    for g in range(n_min):
        rep = rep_of_group[g]
        new_table[g] = {sym: partition[dfa.table[rep][sym]] for sym in dfa.alphabet}
        new_accept[g] = dfa.accept[rep]

    return DFA(
        n_states=n_min,
        start=partition[dfa.start],
        dead=partition[dfa.dead],
        table=new_table,
        accept=new_accept,
        alphabet=dfa.alphabet,
    )


def terminal_states(dfa: DFA) -> FrozenSet[int]:
    """States with no live outgoing transition at all: once you're here, no
    amount of additional input can ever extend the current match further
    (e.g. right after matching a lone ';'). Reaching such a state is always
    final, even mid-file at a chunk boundary -- unlike reaching an accepting
    state for an open-ended pattern (identifier, number, comment, ...),
    which might still grow if the next chunk starts with more of the same."""
    return frozenset(
        s for s in range(dfa.n_states)
        if all(dfa.table[s][sym] == dfa.dead for sym in dfa.alphabet)
    )
