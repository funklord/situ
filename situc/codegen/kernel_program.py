"""The Reed-Solomon decoder, stated once, in no particular language.

Every other derived family is a table and three lines of arithmetic, so
0017's rule -- "a second backend re-spells; it does not re-derive" -- costs
nothing to keep: the content lives in `kernel_math`, and each backend
spells a loop around it. Reed-Solomon inverts that ratio. 26.457 measured
it: 39 lines of backend-neutral derivation against 250 of C, 165 of them
the decoder, and Berlekamp-Massey, the Chien search and Forney's formula
existed nowhere in this repository except as C text inside string
literals. A second backend would have had to transcribe an
error-correcting algorithm by hand, which is the act 0017 was written to
prevent -- and the RS decoder is the specific bug 0017 cites as its
founding evidence.

So the decoder is a PROGRAM here rather than prose: a statement tree a
backend renders. The tree says what the algorithm does; each backend's
`derived.py` says how its language spells a loop, a cast and an array.
Getting the algorithm wrong is now one mistake in one place rather than
one per language, and the Python rendering is executable, so the same
statement tree that generates C can be run against `reedsolo` (26.458).

The language is deliberately the smallest one that expresses this
decoder and nothing else. It is not an intermediate representation for
situ; a second algorithm wanting it is the moment to ask whether it
should be, rather than a reason to widen it now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Lit:
	"""An integer the generator knew: a block length, a root, a count."""

	value: int


@dataclass(frozen=True)
class Name:
	"""A local, a parameter, or the loop variable of an enclosing loop."""

	name: str


@dataclass(frozen=True)
class Index:
	"""`array[at]`. Every index in this program is provably in range, which
	is a claim the renderer is entitled to rely on and a reader is not:
	see `rs_decoder_program` for where each bound comes from."""

	array: str
	at: Expr


@dataclass(frozen=True)
class Gf:
	"""Field arithmetic: `mul`, `inv` or `pow`, which every backend emits
	as three small functions over the same two tables."""

	op: str
	args: tuple[Expr, ...]


@dataclass(frozen=True)
class Binary:
	"""`^ + - * % == != < <= > >=`, in that language's own spelling."""

	op: str
	left: Expr
	right: Expr


Expr: TypeAlias = "Lit | Name | Index | Gf | Binary"


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comment:
	"""Prose, rendered in the target's comment syntax rather than C's."""

	lines: tuple[str, ...]


@dataclass(frozen=True)
class Blank:
	pass


@dataclass(frozen=True)
class Declare:
	"""A scalar local: a field element (`u8`) or a counter (`u32`).

	A backend whose integers do not wrap at those widths masks on
	assignment instead of declaring anything."""

	name: str
	kind: str
	init: Expr


@dataclass(frozen=True)
class Array:
	"""A fixed-length array of field elements, zero at entry."""

	name: str
	length: int


@dataclass(frozen=True)
class Assign:
	target: Expr
	value: Expr


@dataclass(frozen=True)
class XorAssign:
	target: Expr
	value: Expr


@dataclass(frozen=True)
class Increment:
	name: str


@dataclass(frozen=True)
class Loop:
	"""`for var in start .. limit`, by `step`, `limit` included or not.

	A half-open loop over a runtime bound and an inclusive loop over a
	runtime degree are both here because the algorithm uses both, and
	turning one into the other silently is how an off-by-one enters an
	error-correcting code."""

	var: str
	start: Expr
	limit: Expr
	inclusive: bool
	body: tuple[Stmt, ...]
	step: int = 1


@dataclass(frozen=True)
class If:
	cond: Expr
	body: tuple[Stmt, ...]
	orelse: tuple[Stmt, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Return:
	value: Expr


@dataclass(frozen=True)
class Continue:
	pass


Stmt: TypeAlias = ("Comment | Blank | Declare | Array | Assign | XorAssign"
                   " | Increment | Loop | If | Return | Continue")


# ---------------------------------------------------------------------------
# The decoder
# ---------------------------------------------------------------------------


def rs_decoder_program(n: int, nroots: int, first_root: int,
		size: int) -> tuple[Stmt, ...]:
	"""The standard four steps, in the standard order.

	Syndromes say whether anything is wrong; Berlekamp-Massey finds the
	error locator polynomial from them; a Chien search finds its roots,
	which are the error positions; Forney gives the magnitude at each.
	Nothing here is novel, deliberately: a novel error-correcting code is
	the last thing anybody wants, and the value situ adds is that the
	properties in the capability map were derived from the same
	description as this code.

	The block is `block[0 .. n)` and is corrected in place. The result is
	the number of symbols corrected, or -1 where the block holds more
	errors than the code can correct -- which it detects rather than
	guessing at, because a miscorrection is worse than a refusal.

	Every array index below is bounded by a constant of this program:
	`syndrome` and `omega` by `nroots`, `locator`, `previous` and
	`scratch` by `half + 1`, `position` by `degree`, which the
	Berlekamp-Massey step refuses above `half`, and `block` by the length
	check at the top. A backend that cannot index without proving that
	may clamp; it may not silently widen an array.
	"""
	half = nroots // 2

	return (
		Array("syndrome", nroots),
		Array("locator", half + 1),
		Array("previous", half + 1),
		Array("scratch", half + 1),
		Array("omega", nroots),
		Array("position", half),
		Declare("found", "u32", Lit(0)),
		Declare("degree", "u32", Lit(0)),
		Declare("shift", "u32", Lit(1)),
		Declare("discrepancy_last", "u8", Lit(1)),
		Declare("wrong", "u32", Lit(0)),
		Blank(),
		If(Binary("!=", Name("length"), Lit(n)), (Return(Lit(-1)),)),
		Blank(),
		*_syndromes(nroots, first_root),
		*_berlekamp_massey(nroots, half),
		*_chien(size),
		*_forney(nroots, first_root, size),
		Return(Name("found")),
	)


def rs_encoder_program(nroots: int, k: int) -> tuple[Stmt, ...]:
	"""Systematic encode: the message is left alone and the parity is the
	remainder of dividing it, shifted, by the generator.

	`data[0 .. k)` in, `parity[0 .. nroots)` out, and the result is the
	number of parity symbols written -- zero where the caller passed a
	message of the wrong length, which is the only way this can fail.

	Here rather than in a backend for the reason the decoder is: this is
	short enough to transcribe correctly and there is no version of
	"correctly" that survives being written three times.
	"""
	return (
		If(Binary("!=", Name("length"), Lit(k)), (Return(Lit(0)),)),
		Blank(),
		Loop("at", Lit(0), Lit(nroots), False, (
			Assign(Index("parity", Name("at")), Lit(0)),
		)),
		Blank(),
		Loop("at", Lit(0), Name("length"), False, (
			Declare("feedback", "u8",
			        Binary("^", Index("data", Name("at")),
			               Index("parity", Lit(0)))),
			Loop("j", Lit(0), Lit(nroots - 1), False, (
				Assign(Index("parity", Name("j")),
				       Binary("^",
				              Index("parity",
				                    Binary("+", Name("j"), Lit(1))),
				              Gf("mul", (Name("feedback"),
				                         Index("generator",
				                               Binary("-", Lit(nroots - 1),
				                                      Name("j"))))))),
			)),
			Assign(Index("parity", Lit(nroots - 1)),
			       Gf("mul", (Name("feedback"), Index("generator", Lit(0))))),
		)),
		Blank(),
		Return(Lit(nroots)),
	)


def _offset(base: int, var: str) -> Expr:
	"""`base + var`, or just `var` where the schema left `base` at zero.

	Folded here rather than in a backend because a constant the generator
	knew is not a spelling: `alpha^(0 + at)` in three languages is three
	places for a reader to wonder whether the zero means something."""
	if base == 0:
		return Name(var)
	return Binary("+", Lit(base), Name(var))


def _syndromes(nroots: int, first_root: int) -> tuple[Stmt, ...]:
	return (
		Comment(("1. Syndromes: the block evaluated at each root. All zero",
		         "   means nothing is wrong, which is the common case and",
		         "   the cheap one.")),
		Loop("at", Lit(0), Lit(nroots), False, (
			Declare("value", "u8", Lit(0)),
			Declare("root", "u8", Gf("pow", (_offset(first_root, "at"),))),
			Loop("j", Lit(0), Name("length"), False, (
				Assign(Name("value"),
				       Binary("^", Index("block", Name("j")),
				              Gf("mul", (Name("value"), Name("root"))))),
			)),
			Assign(Index("syndrome", Name("at")), Name("value")),
			If(Binary("!=", Name("value"), Lit(0)),
			   (Assign(Name("wrong"), Lit(1)),)),
		)),
		Blank(),
		If(Binary("==", Name("wrong"), Lit(0)), (Return(Lit(0)),)),
		Blank(),
	)


def _berlekamp_massey(nroots: int, half: int) -> tuple[Stmt, ...]:
	ratio = Gf("mul", (Name("discrepancy"),
	                   Gf("inv", (Name("discrepancy_last"),))))

	return (
		Comment(("2. Berlekamp-Massey: the shortest register that makes the",
		         "   syndromes, whose connection polynomial locates the",
		         "   errors.")),
		Assign(Index("locator", Lit(0)), Lit(1)),
		Assign(Index("previous", Lit(0)), Lit(1)),
		Blank(),
		Loop("at", Lit(0), Lit(nroots), False, (
			Declare("discrepancy", "u8", Index("syndrome", Name("at"))),
			Loop("j", Lit(1), Name("degree"), True, (
				XorAssign(Name("discrepancy"),
				          Gf("mul", (Index("locator", Name("j")),
				                     Index("syndrome",
				                           Binary("-", Name("at"),
				                                  Name("j")))))),
			)),
			Blank(),
			If(Binary("==", Name("discrepancy"), Lit(0)), (
				Increment("shift"),
				Continue(),
			)),
			Blank(),
			Loop("j", Lit(0), Lit(half), True, (
				Assign(Index("scratch", Name("j")),
				       Index("locator", Name("j"))),
			)),
			Loop("j", Name("shift"), Lit(half), True, (
				XorAssign(Index("locator", Name("j")),
				          Gf("mul", (ratio,
				                     Index("previous",
				                           Binary("-", Name("j"),
				                                  Name("shift")))))),
			)),
			Blank(),
			If(Binary("<=", Binary("*", Lit(2), Name("degree")), Name("at")), (
				Assign(Name("degree"),
				       Binary("-", Binary("+", Name("at"), Lit(1)),
				              Name("degree"))),
				Loop("j", Lit(0), Lit(half), True, (
					Assign(Index("previous", Name("j")),
					       Index("scratch", Name("j"))),
				)),
				Assign(Name("discrepancy_last"), Name("discrepancy")),
				Assign(Name("shift"), Lit(1)),
			), (
				Increment("shift"),
			)),
		)),
		Blank(),
		Comment(("More errors than the code can locate.",)),
		If(Binary("==", Name("degree"), Lit(0)), (Return(Lit(-1)),)),
		If(Binary(">", Name("degree"), Lit(half)), (Return(Lit(-1)),)),
		Blank(),
	)


def _chien(size: int) -> tuple[Stmt, ...]:
	return (
		Comment(("3. Chien search: every position whose evaluation vanishes",
		         "   is an error. Exhaustive over the block, which is what",
		         "   makes the cost of decoding proportional to the block",
		         "   and not to the errors.")),
		Loop("at", Lit(0), Name("length"), False, (
			# 1 rather than 0: locator[0] is always 1, so the sum below
			# starts from it and runs from the first degree up.
			Declare("value", "u8", Lit(1)),
			Declare("x", "u8",
			        Gf("pow", (Binary("-", Lit(size),
			                          Binary("%",
			                                 Binary("-",
			                                        Binary("-", Name("length"),
			                                               Lit(1)),
			                                        Name("at")),
			                                 Lit(size))),))),
			Declare("term", "u8", Lit(1)),
			Loop("j", Lit(1), Name("degree"), True, (
				Assign(Name("term"), Gf("mul", (Name("term"), Name("x")))),
				XorAssign(Name("value"),
				          Gf("mul", (Index("locator", Name("j")),
				                     Name("term")))),
			)),
			Blank(),
			If(Binary("==", Name("value"), Lit(0)), (
				If(Binary(">=", Name("found"), Name("degree")),
				   (Return(Lit(-1)),)),
				Assign(Index("position", Name("found")), Name("at")),
				Increment("found"),
			)),
		)),
		Blank(),
		Comment(("The locator has roots outside the block.",)),
		If(Binary("!=", Name("found"), Name("degree")), (Return(Lit(-1)),)),
		Blank(),
	)


def _forney(nroots: int, first_root: int, size: int) -> tuple[Stmt, ...]:
	magnitude = Gf("mul", (Name("top"), Gf("inv", (Name("bottom"),))))
	if first_root == 0:
		magnitude = Gf("mul", (magnitude, Name("x")))

	return (
		Comment(("4. Forney: the magnitude at each located position, from",
		         "   the error evaluator over the formal derivative of the",
		         "   locator.")),
		Loop("at", Lit(0), Lit(nroots), False, (
			Declare("value", "u8", Lit(0)),
			Loop("j", Lit(0), Name("at"), True, (
				If(Binary("<=", Name("j"), Name("degree")), (
					XorAssign(Name("value"),
					          Gf("mul", (Index("locator", Name("j")),
					                     Index("syndrome",
					                           Binary("-", Name("at"),
					                                  Name("j")))))),
				)),
			)),
			Assign(Index("omega", Name("at")), Name("value")),
		)),
		Blank(),
		Loop("at", Lit(0), Name("found"), False, (
			Declare("x", "u8",
			        Gf("pow", (Binary("%",
			                          Binary("-",
			                                 Binary("-", Name("length"), Lit(1)),
			                                 Index("position", Name("at"))),
			                          Lit(size)),))),
			Declare("inverse", "u8", Gf("inv", (Name("x"),))),
			Declare("top", "u8", Lit(0)),
			Declare("bottom", "u8", Lit(0)),
			Declare("term", "u8", Lit(1)),
			Blank(),
			Loop("j", Lit(0), Lit(nroots), False, (
				XorAssign(Name("top"),
				          Gf("mul", (Index("omega", Name("j")),
				                     Name("term")))),
				Assign(Name("term"),
				       Gf("mul", (Name("term"), Name("inverse")))),
			)),
			Blank(),
			Comment(("The formal derivative over GF(2) keeps the odd terms.",)),
			Assign(Name("term"), Lit(1)),
			Loop("j", Lit(1), Name("degree"), True, (
				XorAssign(Name("bottom"),
				          Gf("mul", (Index("locator", Name("j")),
				                     Name("term")))),
				Assign(Name("term"),
				       Gf("mul", (Gf("mul", (Name("term"), Name("inverse"))),
				                  Name("inverse")))),
			), step=2),
			Blank(),
			If(Binary("==", Name("bottom"), Lit(0)), (Return(Lit(-1)),)),
			XorAssign(Index("block", Index("position", Name("at"))),
			          magnitude),
		)),
		Blank(),
	)
