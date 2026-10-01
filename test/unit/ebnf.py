"""A recogniser for `doc/grammar.ebnf`, so the grammar is checked against the
corpus rather than against the other copy of itself.

**Why this exists.** `doc/grammar.ebnf` and project.md section 7 are held
together by two tests, and the parser supplies a third witness through its
keyword vocabulary. All three were green while `sealed ... until` and
`u8 c[remaining] max N` were accepted by the compiler and described by
neither grammar (26.542): the document tests compare two copies that agreed
while both were wrong, and the vocabulary tests are keyed on the SPELLING,
where both misses were an existing spelling in a NEW POSITION.

What no witness did was ask whether the grammar can derive the schemas this
repository builds. `test/schema/edges.situ` carries every construct the
worked examples happen not to have, by policy, so a construct the grammar
cannot derive fails here the day it is added.

**The algorithm, and why not Earley.** This is a recogniser rather than a
parser: it answers "which positions can this production reach from here",
returning a SET, which is exact for an ambiguous grammar -- and this grammar
is ambiguous by construction, `array_spec` and `attrs` both opening with
`[`. Earley is the textbook answer and is O(n^3); the corpus is 13450 tokens
with a 3161-token schema in it, which Earley cannot do. A memoised top-down
all-ends search is O(positions * grammar) with the memo, which it can.

Ordered choice is NOT available: a PEG would commit to the first matching
alternative and reject strings the grammar admits. Every alternative is
tried and every end position kept.

**What is delegated, and why that is sound.** The grammar leaves `expr`,
`string`, `char` and `number` undefined and says so -- "the small
productions section 7 uses without ever defining". So they are terminals
here: the three lexical ones match a token kind, and `expr` is handed to
`situc`'s own expression parser, which is the authority on what an
expression is. `ident` and `digits` ARE defined, in terms of `letter` and
`digit`, and those definitions describe CHARACTERS; the tokenizer has
already done that work, so both are overridden as token kinds and their
character-level definitions are unreachable. `_UNDEFINED` asserts the
population, so a fifth undefined name added to the grammar fails here
rather than being silently treated as unmatchable.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from situc.diagnostics import Source, SituError
from situc.lexer import Token, TokenKind, tokenize
from situc.parser import Parser

ROOT = Path(__file__).resolve().parents[2]

#: Names the grammar uses and never defines. Asserted rather than assumed:
#: an unmatchable terminal makes every production above it unmatchable, and
#: a recogniser that cannot match anything reports a clean grammar as a
#: broken corpus.
UNDEFINED = frozenset({"char", "digit", "expr", "letter", "number", "string"})

#: The LEXICAL productions, matched against one token whatever the grammar
#: says about characters.
#:
#: This is the boundary the recogniser has to declare, because the grammar
#: does not have one: it is a CHARACTER grammar throughout. `ident = letter
#: { letter | digit | "_" }` spells an identifier out of characters, and so
#: do `digits`, `uint = "u" digits` and `sint = "i" digits`. The tokenizer
#: has already done all of that, so each is one token here.
#:
#: `uint` and `sint` are the ones that cost a debugging round, and they are
#: worth the note. A field parses without them -- `type_ref = scalar_type |
#: ident` falls through to `ident`, so `u8 lead;` matched as an identifier --
#: while `tag_field` names `scalar_type` directly and could not match `u8` at
#: all. The two-level mismatch was therefore invisible on the construct it
#: appears in most and fatal on the one it appears in once.
#:
#: `float`, `bitfield`, `"bool"` and `"byte"` need nothing: `f32` and `bit`
#: are single tokens already.
def _kind(wanted: TokenKind) -> Callable[[Token], bool]:
	return lambda token: token.kind is wanted


def _named_width(pattern: str) -> Callable[[Token], bool]:
	"""`u8`, `i32`: one IDENT token, where the grammar spells characters."""
	matcher = re.compile(pattern)
	return lambda token: (token.kind is TokenKind.IDENT
	                      and matcher.fullmatch(token.text) is not None)


BY_TOKEN: dict[str, Callable[[Token], bool]] = {
	"ident":  _kind(TokenKind.IDENT),
	"digits": _kind(TokenKind.INT),
	"number": _kind(TokenKind.INT),
	"string": _kind(TokenKind.STRING),
	"char":   _kind(TokenKind.CHAR),
	"uint":   _named_width(r"u[0-9]+"),
	"sint":   _named_width(r"i[0-9]+"),
}


# -- the EBNF itself ---------------------------------------------------------

@dataclass(frozen=True)
class Lit:
	"""A quoted terminal: `"struct"`, `"["`."""
	text: str

@dataclass(frozen=True)
class Ref:
	"""A production name."""
	name: str

@dataclass(frozen=True)
class Any:
	"""`? any ?` -- one token of anything. Only `stage` uses it."""

@dataclass(frozen=True)
class Seq:
	items: tuple[object, ...]

@dataclass(frozen=True)
class Alt:
	options: tuple[object, ...]

@dataclass(frozen=True)
class Opt:
	body: object

@dataclass(frozen=True)
class Rep:
	body: object


class EbnfError(Exception):
	pass


_TOKEN = re.compile(r'"[^"]*"|\?[^?]*\?|[A-Za-z_][A-Za-z0-9_]*|[=;|\[\]{}()]')


def parse_ebnf(text: str) -> dict[str, object]:
	"""Every production, as name -> node.

	Comments are stripped first. A production runs from a name at column
	zero to the `;` that closes it, which is the same shape
	`test_grammar_sync` already relies on.
	"""
	text = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.S)
	out: dict[str, object] = {}
	for found in re.finditer(r"^([a-z_][a-z0-9_]*)\s*=(.*?);\s*$",
	                         text, re.M | re.S):
		name, body = found.group(1), found.group(2)
		words = _TOKEN.findall(body)
		node, rest = _alt(words, 0)
		if rest != len(words):
			raise EbnfError(f"{name}: trailing {words[rest:][:4]}")
		out[name] = node
	if not out:
		raise EbnfError("no productions parsed")
	return out


def _alt(words: list[str], at: int) -> tuple[object, int]:
	options = []
	while True:
		node, at = _seq(words, at)
		options.append(node)
		if at < len(words) and words[at] == "|":
			at += 1
			continue
		return (options[0] if len(options) == 1 else Alt(tuple(options))), at


def _seq(words: list[str], at: int) -> tuple[object, int]:
	items: list[object] = []
	while at < len(words) and words[at] not in ("|", "]", "}", ")"):
		word = words[at]
		if word in ("[", "{", "("):
			close = {"[": "]", "{": "}", "(": ")"}[word]
			body, at = _alt(words, at + 1)
			if at >= len(words) or words[at] != close:
				raise EbnfError(f"expected {close}, found {words[at:][:3]}")
			at += 1
			if word == "[":
				items.append(Opt(body))
			elif word == "{":
				items.append(Rep(body))
			else:
				items.append(body)		# a group is its own contents
			continue
		at += 1
		if word.startswith('"'):
			items.append(Lit(word[1:-1]))
		elif word.startswith("?"):
			items.append(Any())
		else:
			items.append(Ref(word))
	return (items[0] if len(items) == 1 else Seq(tuple(items))), at


# -- the recogniser ----------------------------------------------------------

@dataclass
class Recogniser:
	"""Which end positions each production can reach from each start.

	`furthest` is kept for diagnostics only: on a failure the interesting
	number is how far the grammar got, since the token after it is where the
	grammar and the parser disagree.
	"""
	tokens: list[Token]
	grammar: dict[str, object]
	expr_end: dict[int, int]
	furthest: int = 0
	_memo: dict[tuple[int, str], frozenset[int]] = dc_field(default_factory=dict)
	_busy: set[tuple[int, str]] = dc_field(default_factory=set)

	def ends(self, node: object, at: int) -> frozenset[int]:
		self.furthest = max(self.furthest, at)

		if isinstance(node, Lit):
			token = self.tokens[at]
			hit = (token.kind in (TokenKind.IDENT, TokenKind.SYMBOL)
			       and token.text == node.text)
			return frozenset({at + 1}) if hit else frozenset()

		if isinstance(node, Any):
			return (frozenset({at + 1})
			        if self.tokens[at].kind is not TokenKind.EOF
			        else frozenset())

		if isinstance(node, Ref):
			return self._ref(node.name, at)

		if isinstance(node, Alt):
			out: set[int] = set()
			for option in node.options:
				out |= self.ends(option, at)
			return frozenset(out)

		if isinstance(node, Seq):
			here = {at}
			for item in node.items:
				step: set[int] = set()
				for pos in here:
					step |= self.ends(item, pos)
				if not step:
					return frozenset()
				here = step
			return frozenset(here)

		if isinstance(node, Opt):
			return frozenset({at} | set(self.ends(node.body, at)))

		if isinstance(node, Rep):
			# Iteratively, so a nullable body cannot spin and so no left
			# recursion is introduced by the desugaring -- `{ X }` as
			# `rep = rep X | eps` would be left-recursive by construction.
			reached = {at}
			edge = {at}
			while edge:
				grown: set[int] = set()
				for pos in edge:
					grown |= self.ends(node.body, pos)
				edge = grown - reached
				reached |= edge
			return frozenset(reached)

		raise EbnfError(f"unknown node {node!r}")

	def _ref(self, name: str, at: int) -> frozenset[int]:
		matches = BY_TOKEN.get(name)
		if matches is not None:
			return (frozenset({at + 1})
			        if matches(self.tokens[at]) else frozenset())
		if name == "expr":
			end = self.expr_end.get(at)
			return frozenset({end}) if end is not None else frozenset()

		key = (at, name)
		if key in self._memo:
			return self._memo[key]
		if key in self._busy:
			# A cycle at one position. This grammar has none -- every
			# recursive path consumes a bracket or a keyword first -- and
			# returning empty rather than looping means a future left
			# recursion under-reports instead of hanging.
			return frozenset()
		body = self.grammar.get(name)
		if body is None:
			raise EbnfError(f"no production for `{name}`")
		self._busy.add(key)
		try:
			out = self.ends(body, at)
		finally:
			self._busy.discard(key)
		self._memo[key] = out
		return out


def expression_spans(source: Source, tokens: list[Token]) -> dict[int, int]:
	"""Where `situc`'s own expression parser says an expression ends.

	One entry per position an expression can start at, so the recogniser can
	treat `expr` as a single multi-token terminal. Delegated rather than
	modelled because the grammar declines to define `expr` at all, and a
	second expression grammar written here would be a second thing to be
	wrong -- and would be written by the same hand as the first, which
	`evidence.md` says is one witness.
	"""
	parser = Parser(source)
	out: dict[int, int] = {}
	for at in range(len(tokens)):
		if tokens[at].kind is TokenKind.EOF:
			continue
		parser.pos = at
		try:
			parser.parse_expr()
		except (SituError, IndexError, RecursionError):
			continue
		if parser.pos > at:
			out[at] = parser.pos
	return out


@dataclass(frozen=True)
class Verdict:
	ok: bool
	furthest: int
	tokens: list[Token]

	def where(self) -> str:
		"""The token the grammar could not get past, with its neighbours."""
		at   = min(self.furthest, len(self.tokens) - 1)
		near = self.tokens[max(0, at - 3):at + 4]
		span = self.tokens[at].span
		return (f"line {span.line}: ..."
		        + " ".join(t.text for t in near)
		        + f"...  (stops before `{self.tokens[at].text}`)")


def recognise(path: Path, grammar: dict[str, object]) -> Verdict:
	"""Whether the grammar derives this schema, whole, to end of file."""
	source = Source(str(path), path.read_text(encoding="utf-8"))
	tokens = tokenize(source)
	engine = Recogniser(tokens, grammar, expression_spans(source, tokens))
	eof    = len(tokens) - 1
	reached = engine.ends(Ref("schema"), 0)
	return Verdict(eof in reached, engine.furthest, tokens)
