"""Ways of writing a schema fact that no target language decides.

One function so far, and the reason it is here rather than in a backend: how a
delimiter reads is a property of the delimiter. Three headers describing one
schema should spell CRLF the same way, and the second backend to need this
copied it before this module existed.
"""

from __future__ import annotations

import re
from collections.abc import Callable


def render_delimiter(delim: bytes) -> str:
	"""A delimiter as a reader of the generated code would write it.

	`"\\r\\n"` rather than `{0x0D, 0x0A}`: the comment is there to be checked
	against the specification somebody is implementing, and that specification
	says CRLF.
	"""
	named = {0x0D: "\\r", 0x0A: "\\n", 0x09: "\\t", 0x00: "\\0"}
	shown = "".join(named.get(byte, chr(byte) if 0x20 <= byte < 0x7F
	                          else f"\\x{byte:02x}")
	                for byte in delim)
	return f'`"{shown}"`'


def over_fields(names: list[str], source: str,
		getter: Callable[[str], str]) -> str:
	"""A schema expression rewritten so each field name is a read of it.

	`(hdr_ext_len + 1) * 8 - 2` becomes the same arithmetic over whatever the
	backend calls reading `hdr_ext_len`. The expression's shape is the
	schema's and identical in every target; only the read differs, which is
	the same split `situc.invariant` makes for the same reason.

	**Longest name first**, or `len` rewrites the `len` inside `hdr_ext_len`
	and the result names a getter that does not exist. That is not
	hypothetical: those two fields sit side by side in an IPv6 extension
	header, which is the schema this was written for.

	**One pass, not one per name.** Sequential substitution can rewrite what
	an earlier substitution wrote: every backend's getter mentions the view it
	reads through, so a schema with a field called `view` or `self` would have
	had that word replaced inside the reads already emitted. Nothing in the
	tree is named either, which is exactly the kind of latent collision
	invariant 29 is about -- generated code shares a namespace with names the
	schema chose. A single alternation cannot match its own output.
	"""
	rewritten = source
	if names:
		pattern   = "|".join(re.escape(name)
		                     for name in sorted(names, key=len, reverse=True))
		rewritten = re.sub(rf"\b(?:{pattern})\b",
		                   lambda hit: getter(hit.group(0)), source)

	# And nothing left over. This substitutes what it is given and passed the
	# rest through, so a name the caller did not list reached the target
	# verbatim: Lua saw a global (invariant 60, fixed there), and C saw `if
	# (which != 17u)` with no `which` in scope -- a discriminant behind a
	# delimited member, which `readable_names` used to leave out. A rewriter
	# that cannot rewrite a name has not got an answer, and saying so is the
	# difference between a caller that declines the member and generated code
	# that does not build.
	leftover = _unrewritten(source, names)
	if leftover is not None:
		raise UnknownName(leftover)
	return rewritten


class UnknownName(KeyError):
	"""An expression names something the caller cannot read."""


def _unrewritten(source: str, names: list[str]) -> str | None:
	"""The first identifier in `source` that is neither a name nor a builtin.

	Dotted paths count as one name, so `hdr.n` is looked for whole rather
	than as `hdr` and `n` -- which is what the caller lists.
	"""
	known = set(names) | set(CALL_ARITY)
	# Not inside a number: `0x11` contains `x11`, and a hex literal is the
	# commonest thing in a `while` condition. The lookbehind is what keeps
	# this reading identifiers rather than substrings.
	for hit in re.finditer(
			r"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_]*"
			r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*", source):
		name = hit.group(0)
		if name in known:
			continue
		# A dotted name whose head is known is a read of that name's field,
		# which the caller listed under the whole path or not at all.
		if any(name == one or name.startswith(f"{one}.") for one in known):
			continue
		return name
	return None


def translate_operators(source: str, *, conj: str, disj: str,
		ne: str, neg: str, div: str = "/") -> str:
	"""A schema condition in a target that does not spell C's operators.

	The schema's operators are C's, which is a choice the language made once
	and C, C++ and Rust are all happy with. Python spells three of them in
	words and Lua spells four, so both needed this -- and both needed it in
	the same order, which is the reason it is one function.

	**`!=` before `!`.** Rewriting negation first eats the `!` of an
	inequality and leaves `not =`, which is not an expression in either
	language. It is one line either way and the wrong line reads fine.

	**`div` is the same shape and was not handled.** `/` is integer division
	in C, C++ and Rust and float division in Python and Lua, so `body[n / 2]`
	generated a Python slice bound of `2.5` -- `TypeError: slice indices must
	be integers` -- and an offset that stayed a float from there on. The
	docstring above this one had named that hazard as an analogy for the `||`
	it did fix, which is as close as a comment gets to being a bug report.
	Both spell floor division `//`, and floor and truncation agree wherever
	both operands are non-negative: every size is, since a size whose lower
	bound the solver cannot derive is refused (section 8.5). A signed operand
	inside a *condition* is where the two still part.
	"""
	source = source.replace("!=", "\x00")		# park it out of reach of `!`
	source = re.sub(r"!", neg, source)
	source = source.replace("\x00", ne)
	source = source.replace("&&", conj).replace("||", disj)
	if div != "/":
		source = re.sub(r"(?<![/*])/(?![/*])", div, source)
	return re.sub(r"\s+", " ", source).strip()


#: How many arguments each value builtin takes. `situc.expr` is authoritative
#: about which exist; this is what a backend has to be able to spell.
CALL_ARITY = {"min": 2, "max": 2, "align_up": 2}


def expand_calls(source: str, spell: Callable[[str, list[str]], str]) -> str:
	"""Rewrite each builtin call in a rendered expression as the target
	spells it.

	The expression reaches a backend as source text, and every backend passed
	it through unchanged -- so `align_up(nla_len, 4)` arrived in C, C++,
	Python and Rust as a call to a function that exists in none of them, and
	the generated code did not compile. It had never been noticed because no
	schema in the repository used a builtin in a size, which is a fact about
	which schemas got written rather than a fact about the language.

	`spell` is the backend's, because this is the one part of an expression
	that genuinely differs: C has a conditional operator and Python has
	`min`. What the *arithmetic* is stays here, so four targets cannot round
	an alignment three ways.

	Called after the field names have been rewritten, so the arguments are
	already reads. They can therefore contain parentheses and, where a
	builtin nests, commas -- both of which is why this counts depth rather
	than reaching for a regex.
	"""
	for name in sorted(CALL_ARITY, key=len, reverse=True):
		source = _expand_one(source, name, spell)
	return source


def _expand_one(source: str, name: str,
		spell: Callable[[str, list[str]], str]) -> str:
	"""One pass, left to right, never re-reading what it has written.

	Rescanning from the start was an infinite loop the moment a target spells
	a builtin with the builtin's own name: Python's `min` *is* `min`, so the
	expansion found its own output and expanded it again. `situc build` hung
	rather than emitting anything wrong, which is the good failure -- and it
	took a schema that actually calls one to find it (invariant 15).

	Arguments are expanded before the call around them, so a nested builtin
	still gets its own spelling.
	"""
	pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
	pieces: list[str] = []
	cursor = 0

	while True:
		found = pattern.search(source, cursor)
		if found is None:
			pieces.append(source[cursor:])
			return "".join(pieces)

		args, end = _arguments(source, found.end())
		if args is None or end is None:
			pieces.append(source[cursor:])
			return "".join(pieces)	# unbalanced: leave it for the target

		pieces.append(source[cursor:found.start()])
		pieces.append(spell(name, [expand_calls(arg, spell) for arg in args]))
		cursor = end


def _rounded(value: str, unit: str, one: str, div: str) -> str:
	"""`align_up`, as arithmetic. One definition, four targets.

	The same rounding `situc.expr` folds a constant with, which is the reason
	it is written once: a generated `align_up` that rounds differently from
	the one the solver folded would make a schema mean two things depending on
	whether the alignment was a literal.
	"""
	return f"((({value}) + ({unit}) - {one}) {div} ({unit}) * ({unit}))"


def c_spelling(name: str, args: list[str]) -> str:
	"""C and C++: a conditional operator, and unsigned literals for the flags
	this project builds generated code under."""
	left, right = args
	if name == "align_up":
		return _rounded(left, right, "1u", "/")
	comparison = "<" if name == "min" else ">"
	return f"((({left}) {comparison} ({right})) ? ({left}) : ({right}))"


def rust_spelling(name: str, args: list[str]) -> str:
	"""`core::cmp` rather than a conditional expression.

	`if a < b { (a) } else { (b) }` is correct Rust and `-D warnings` refuses
	it -- "unnecessary parentheses around block return value" -- and dropping
	the parentheses would change what the arithmetic means. Invariant 23 is
	the reason that matters: generated code that warns teaches a reader to
	ignore warnings.
	"""
	left, right = args
	if name == "align_up":
		return _rounded(left, right, "1", "/")
	return f"core::cmp::{name}({_unwrap(left)}, {_unwrap(right)})"


def _unwrap(text: str) -> str:
	"""One layer of parentheses, where they wrap the whole expression.

	The field renderer parenthesises every read, and `-D warnings` calls a
	parenthesised function argument unnecessary. Only where the outer pair
	matches, so `(a) + (b)` is left alone.
	"""
	if not (text.startswith("(") and text.endswith(")")):
		return text
	depth = 0
	for index, char in enumerate(text):
		depth += 1 if char == "(" else -1 if char == ")" else 0
		if depth == 0 and index < len(text) - 1:
			return text
	return text[1:-1]


def python_spelling(name: str, args: list[str]) -> str:
	left, right = args
	if name == "align_up":
		return _rounded(left, right, "1", "//")
	return f"{name}(({left}), ({right}))"


def lua_spelling(name: str, args: list[str]) -> str:
	left, right = args
	if name == "align_up":
		return f"(math.floor((({left}) + ({right}) - 1) / ({right})) * ({right}))"
	return f"math.{name}(({left}), ({right}))"


def _arguments(source: str, start: int) -> tuple[list[str] | None, int | None]:
	"""The comma-separated arguments of a call whose `(` is at `start - 1`."""
	depth = 1
	args: list[str] = []
	piece = start
	for index in range(start, len(source)):
		char = source[index]
		if char in "([":
			depth += 1
		elif char in ")]":
			depth -= 1
			if depth == 0:
				args.append(source[piece:index].strip())
				return args, index + 1
		elif char == "," and depth == 1:
			args.append(source[piece:index].strip())
			piece = index + 1
	return None, None
