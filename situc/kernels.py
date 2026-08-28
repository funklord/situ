"""Derive a property signature from a kernel description (section 13.4).

The difference between the two codec tiers, stated exactly: a tier-1 signature
is *declared* and trusted, and a tier-2 signature is *computed* from a
description the compiler could also generate the implementation from. A codec
whose properties the compiler can work out is one whose properties it need not
take on faith.

Nothing here touches the lattice. Section 26.12 is explicit that no propagation
rule changes in this phase, and it holds because the property signature is the
only interface (13.1): the lattice reads the same nine properties whether a
human wrote them or this module worked them out.

The survey behind 13.4 is what makes this bounded. Essentially every line code,
FEC, scrambler and framing code in practical use is one of six families or a
pipeline of them, so deriving properties is six functions rather than an open
problem.

Where a schema declares a property *and* a kernel, the two must agree. A
disagreement is an error rather than a precedence rule: one of them is wrong,
and quietly preferring either would hide which.
"""

from __future__ import annotations

from math import lcm

from dataclasses import dataclass, replace

from situc import ast
from situc.diagnostics import error

# Properties a derived signature sets. Anything absent from a family's result
# keeps the conservative default a declaration would have had.
DERIVED_PROPERTIES = (
	"expansion", "expansion_add", "ratio", "seekable", "granularity",
	"granularity_size", "systematic", "invertible", "deterministic",
	"error_propagating",
)


@dataclass(frozen=True)
class Derived:
	"""What a kernel implies, in the vocabulary of 13.2."""

	expansion: ast.Expansion        = ast.Expansion.PRESERVING
	expansion_add: int              = 0
	ratio: tuple[int, int] | None   = None
	seekable: ast.Seekable          = ast.Seekable.NONE
	granularity: ast.Granularity    = ast.Granularity.STREAM
	granularity_size: int | None    = None
	systematic: bool                = False
	invertible: bool                = False
	deterministic: bool             = True
	error_propagating: bool         = False


def derive(decl: ast.CodecDecl) -> Derived:
	"""The signature a kernel implies."""
	kernel = decl.kernel
	assert kernel is not None, "callers check for a kernel first"

	return _FAMILIES[kernel.family](decl, kernel)


def apply_to(decl: ast.CodecDecl) -> ast.CodecDecl:
	"""Fill a codec's signature in from its kernel, checking what was declared.

	A property the author also wrote has to match. Preferring the declaration
	would let a wrong one through unchallenged, and preferring the kernel would
	silently ignore what somebody took the trouble to state -- so a
	disagreement is an error that names both.
	"""
	if decl.kernel is None:
		return decl

	derived  = derive(decl)
	declared = _explicit(decl)

	for name in sorted(declared):
		mine  = getattr(derived, name)
		yours = getattr(decl, name)
		if mine == yours:
			continue

		raise error(
			f"`{decl.name}` declares `{_render(name, yours)}` but its kernel "
			f"implies `{_render(name, mine)}`",
			decl.kernel.span,
			label = f"the {decl.kernel.family.value} kernel implies "
			        f"{_render(name, mine)}",
			notes = [
				"a derived codec's properties are computed from its kernel, so a "
				"declaration that disagrees is one of the two being wrong",
				"drop the declared property to take the derived one, or fix the "
				"kernel if the declaration is what you meant",
			],
		)

	return replace(decl, **{name: getattr(derived, name)
	                        for name in DERIVED_PROPERTIES})


def _explicit(decl: ast.CodecDecl) -> set[str]:
	"""Properties the author wrote, as opposed to ones that defaulted.

	Recovered by comparing against a bare signature: the parser does not record
	which properties were written, and a property equal to its own default was
	not worth arguing about anyway.
	"""
	bare = ast.CodecDecl(decl.span, decl.name)
	return {name for name in DERIVED_PROPERTIES
	        if getattr(decl, name) != getattr(bare, name)}


def _render(name: str, value: object) -> str:
	if isinstance(value, (ast.Expansion, ast.Seekable, ast.Granularity)):
		return f"{name} = {value.value}"
	if isinstance(value, bool):
		return name if value else f"not {name}"
	return f"{name} = {value}"


# ---------------------------------------------------------------------------
# The families
# ---------------------------------------------------------------------------


def _table(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""An input symbol maps to an output symbol.

	Manchester, 4b5b, NRZI in its table form, Gray, BCD. The ratio is
	exact because every symbol is the same width, which is what keeps an
	interior position a linear function of an input position -- and the reason
	these keep static addressing where a stuffing code does not.

	**8b10b is not one of these, and was listed here as though it were.** Its
	ratio is table-like -- 10:8, exact -- but its encoder carries running
	disparity, so one input byte has two valid output symbols and which one is
	emitted depends on everything encoded before it. A table kernel derives
	`deterministic` unconditionally, and by this project's own definition --
	"repeated encoding of identical input is byte-identical" -- 8b10b is not,
	which propagates a false `canonical` through the lattice. Nothing hits it
	today because no 8b10b table is implemented; the classification was the
	error, and closing it is a language question rather than a table entry.

	Not systematic: the input symbols do not appear verbatim in the output, so
	nothing can be read without decoding.
	"""
	inputs  = _positive(kernel, "input_bits", decl)
	outputs = _positive(kernel, "output_bits", decl)

	_unambiguous(decl, kernel)

	# `pad` says the code emits whole groups: a group is the smallest run of
	# input that is both a whole number of bytes and a whole number of symbols,
	# so a partial one at the end is filled out rather than truncated. That is
	# what base32 and base64 do and what base16 never has to, because four bits
	# divide a byte exactly.
	padded = kernel.argument("pad") is not None

	# A single-bit input is a bit code; anything wider is a symbol code. That
	# is the distinction the hand-written library draws -- Manchester is
	# `bit(1)` and 4b5b is `symbol(4)` -- and it is the useful one: a symbol is
	# a unit somebody has to align to. A padded code is coarser still: the unit
	# somebody has to align to is the group, not the symbol.
	group = lcm(BITS_PER_SYMBOL, inputs)

	return Derived(
		expansion        = (ast.Expansion.RATIO_PADDED if padded
		                    else ast.Expansion.RATIO_EXACT),
		ratio            = (outputs, inputs),
		seekable         = ast.Seekable.LINEAR,
		granularity      = (ast.Granularity.BLOCK if padded
		                    else ast.Granularity.BIT if inputs == 1
		                    else ast.Granularity.SYMBOL),
		granularity_size = group // BITS_PER_SYMBOL if padded else inputs,
		systematic       = False,
		invertible       = True,
		deterministic    = True,
	)


#: Code names that are two codes. `manchester` is the instance and the reason
#: this exists: IEEE 802.3 and G.E. Thomas both call themselves Manchester and
#: are bit-inverses of each other, so a receiver built on one reads a sender
#: built on the other as the complement of what was sent -- plausible bytes,
#: no error, and nothing at run time can tell. rflab, a radio project written
#: against real hardware, makes the same choice a compile-time option, which is
#: the evidence that a practitioner has to make it.
#:
#: Invariant 9: situ never takes a silent default where the wrong choice is
#: undetectable at run time. The remedy is to say which.
AMBIGUOUS_CODES: dict[str, tuple[str, ...]] = {
	"manchester": ("manchester_802_3", "manchester_thomas"),
}


def _unambiguous(decl: ast.CodecDecl, kernel: ast.Kernel) -> None:
	"""Refuse a code name that names more than one code."""
	named = kernel.argument("code")
	if not isinstance(named, ast.NameRef):
		return

	choices = AMBIGUOUS_CODES.get(named.name)
	if choices is None:
		return

	raise error(
		f"`{named.name}` names {len(choices)} codes, not one",
		kernel.span,
		f"`{decl.name}` does not say which",
		[f"the candidates are {', '.join(f'`{one}`' for one in choices)}, and "
		 "they are bit-inverses of each other",
		 "a decoder built on the wrong one returns the complement of what was "
		 "sent: plausible bytes, no error, and nothing at run time to notice",
		 f"say `code = {choices[0]}` or `code = {choices[1]}`"],
	)


def _polynomial(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""A generator polynomial over GF(2), plus init, reflection and xorout.

	Every CRC variant. The parity is appended and the data is left verbatim, so
	the form is systematic and the expansion is a fixed number of bytes -- which
	is what lets a field under one be read with no decode at all.
	"""
	# A polynomial over an extension field is a Reed-Solomon or BCH code, not
	# a CRC: the parity is symbols rather than a digest, and it comes back --
	# so the code is invertible where a CRC is not, and it corrects rather than
	# merely detecting, which is what makes a burst beyond its capacity
	# propagate.
	if kernel.argument("field") is not None:
		return _reed_solomon(decl, kernel)

	width = _positive(kernel, "width", decl)
	if width % 8:
		raise error(
			f"`{decl.name}` has a {width}-bit polynomial kernel",
			kernel.span,
			label = "not a whole number of bytes",
			notes = ["a checksum is appended as bytes; a width that is not a "
			         "multiple of eight has no byte string to append"],
		)

	return Derived(
		expansion        = ast.Expansion.FIXED_ADD,
		expansion_add    = width // 8,
		seekable         = ast.Seekable.LINEAR,
		granularity      = ast.Granularity.BLOCK,
		granularity_size = None,
		systematic       = True,
		invertible       = False,		# a digest cannot be undone
		deterministic    = True,
	)


def _reed_solomon(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""A polynomial code over GF(2^m): Reed-Solomon, and BCH by the same route.

	Systematic, so the message symbols sit verbatim ahead of the parity and a
	reader that trusts the block takes them with no decode at all -- which is
	the property section 13.2 calls the highest-value one and the reason these
	are worth describing rather than treating as opaque.
	"""
	field = _positive(kernel, "field", decl)
	n     = _positive(kernel, "n", decl)
	k     = _positive(kernel, "k", decl)

	if field & (field - 1) or field < 4:
		raise error(
			f"`{decl.name}` has a field of {field} elements",
			kernel.span,
			label = "not a power of two",
			notes = ["GF(2^m) has 2^m elements; `field = 256` is the byte-wide "
			         "field every practical code uses"],
		)

	if k >= n:
		raise error(
			f"`{decl.name}` encodes {k} symbols into {n}",
			kernel.span,
			label = "a code with no parity corrects nothing",
			notes = [f"`n` is the block length and `k` the message length; "
			         f"n - k = {n - k} parity symbols correct {(n - k) // 2} "
			         "errors"],
		)

	if n > field - 1:
		raise error(
			f"`{decl.name}` has a block of {n} symbols over GF({field})",
			kernel.span,
			label = f"the field has only {field - 1} non-zero elements",
			notes = ["each symbol position needs a distinct field element, so "
			         f"a block cannot exceed {field - 1}"],
		)

	symbol_bits = field.bit_length() - 1

	return Derived(
		expansion        = ast.Expansion.FIXED_ADD,
		expansion_add    = (n - k) * symbol_bits // BITS_PER_SYMBOL,
		seekable         = ast.Seekable.LINEAR,
		granularity      = ast.Granularity.BLOCK,
		granularity_size = n,
		systematic       = True,
		invertible       = True,	# the message comes back; a digest does not
		deterministic    = True,
		error_propagating = True,	# a burst past the capacity spoils the block
	)


BITS_PER_SYMBOL = 8


def _linear_block(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""A generator matrix over GF(2): Hamming, extended Hamming, LDPC.

	Systematic exactly when the matrix is in standard form, which is the
	property worth deriving rather than trusting: it decides whether the data
	bits sit verbatim at computable positions, and a signature that claimed it
	wrongly would promise reads that decode nothing.
	"""
	n = _positive(kernel, "n", decl)
	k = _positive(kernel, "k", decl)

	if k > n:
		raise error(
			f"`{decl.name}` encodes {k} bits into {n}",
			kernel.span,
			label = "a block code cannot shrink",
			notes = ["`n` is the codeword length and `k` the message length"],
		)

	# The unit is the message block, `k` bits, since that is the smallest
	# amount that can be encoded independently. Not error-propagating: damage
	# is contained to the codeword it lands in, which is what a block code is
	# for -- unlike a scrambler, where it runs on.
	return Derived(
		expansion        = ast.Expansion.RATIO_EXACT,
		ratio            = (n, k),
		seekable         = ast.Seekable.LINEAR,
		granularity      = (ast.Granularity.BIT if k < 8
		                    else ast.Granularity.SYMBOL),
		granularity_size = k,
		systematic       = kernel.flag("standard_form"),
		invertible       = True,
		deterministic    = True,
	)


def _shift_register(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""Taps, a feedback source, and an initial state.

	The feedback source is the whole derivation. Feedback from the input is an
	additive scrambler: the keystream at position N does not depend on what came
	out, so the encoder can be started anywhere and a corrupt bit spoils only
	itself. Feedback from the output is multiplicative: neither is true, and the
	signature has to say so.
	"""
	source = _name_of(kernel.argument("feedback"))
	if source not in ("input", "output"):
		raise error(
			f"`{decl.name}` does not say where its feedback comes from",
			kernel.span,
			label = "expected `feedback = input` or `feedback = output`",
			notes = ["it is the property the rest are derived from: feedback "
			         "from the input is seekable and self-synchronising, and "
			         "feedback from the output is neither"],
		)

	from_input = source == "input"
	return Derived(
		expansion        = ast.Expansion.PRESERVING,
		seekable         = ast.Seekable.LINEAR if from_input else ast.Seekable.NONE,
		granularity      = ast.Granularity.BIT,
		granularity_size = 1,
		systematic       = False,
		invertible       = True,
		deterministic    = True,
		error_propagating = not from_input,
	)


def _permutation(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""An index mapping: block and convolutional interleavers.

	A bijection over positions, so nothing is added or lost and every byte is
	still reachable -- but not in order, which is exactly what
	`seekable = permuted` exists to say. Random access survives; sequential
	prefetch does not.
	"""
	# `rows` and `columns` describe a block interleaver, which is the form the
	# implementation can be generated from; a bare `span` describes the extent
	# of some permutation without saying which, which is enough for the
	# properties and not enough for the code.
	if kernel.argument("rows") is not None:
		rows = _positive(kernel, "rows", decl)
		_positive(kernel, "columns", decl)
		if rows < 2:
			raise error(
				f"`{decl.name}` interleaves over one row",
				kernel.span,
				label = "an interleaver of one row is the identity",
				notes = ["the point of interleaving is to spread a burst across "
				         "codewords; one row spreads it across one"],
			)
	else:
		_positive(kernel, "span", decl)

	return Derived(
		expansion     = ast.Expansion.PRESERVING,
		seekable      = ast.Seekable.PERMUTED,
		granularity   = ast.Granularity.BYTE,
		systematic    = False,
		invertible    = True,
		deterministic = True,
	)


def _stuffing(decl: ast.CodecDecl, kernel: ast.Kernel) -> Derived:
	"""A trigger predicate and an insertion rule: HDLC, COBS, SLIP.

	How many bytes an encoded region takes depends on its content, so interior
	addressing is gone entirely. This is why real protocols apply stuffing only
	at the outermost layer, and why the advisor should say so when it sees one
	applied inward.
	"""
	worst = _positive(kernel, "worst_case", decl)
	over  = _positive(kernel, "per", decl)

	# What the trigger examines. HDLC counts bits and COBS scans a stream of
	# bytes, and the difference is real: it decides the smallest amount a
	# decoder has to take in before it can act.
	unit = _name_of(kernel.argument("unit")) or "stream"
	if unit not in STUFFING_UNITS:
		raise error(
			f"`{decl.name}` stuffs over an unknown unit `{unit}`",
			kernel.span,
			label = "expected `bit`, `byte` or `stream`",
			notes = ["it is what the trigger examines: HDLC counts bits, COBS "
			         "scans a stream of bytes"],
		)

	granularity, size = STUFFING_UNITS[unit]

	return Derived(
		expansion        = ast.Expansion.RATIO_BOUNDED,
		ratio            = (worst, over),
		seekable         = ast.Seekable.NONE,
		granularity      = granularity,
		granularity_size = size,
		systematic       = False,
		invertible       = True,
		deterministic    = True,
	)


STUFFING_UNITS: dict[str, tuple[ast.Granularity, int | None]] = {
	"bit":    (ast.Granularity.BIT, 1),
	"byte":   (ast.Granularity.BYTE, None),
	"stream": (ast.Granularity.STREAM, None),
}


_FAMILIES = {
	ast.KernelFamily.TABLE:       _table,
	ast.KernelFamily.POLYNOMIAL:  _polynomial,
	ast.KernelFamily.LINEAR:      _linear_block,
	ast.KernelFamily.SHIFT:       _shift_register,
	ast.KernelFamily.PERMUTATION: _permutation,
	ast.KernelFamily.STUFFING:    _stuffing,
}


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


def compose(decl: ast.CodecDecl, stages: list[ast.CodecDecl]) -> ast.CodecDecl:
	"""`rs |> interleave |> manchester`: pointwise and conservative (13.4).

	Conservative in every direction. The pipeline is seekable only if every
	stage is, systematic only if every stage is, deterministic only if every
	stage is, and error-propagating if any stage is. The expansion is the
	product of the stages'.

	A pipeline claiming more than its weakest stage would be a signature that
	lies, and the lattice believes signatures.
	"""
	if not stages:
		raise error(f"`{decl.name}` is an empty pipeline", decl.span,
		            label="no stages")

	expansion, ratio, add = _compose_expansion(decl, stages)

	return replace(
		decl,
		expansion         = expansion,
		expansion_add     = add,
		ratio             = ratio,
		seekable          = _weakest_seekable(stages),
		granularity       = _coarsest_granularity(stages),
		granularity_size  = None,
		systematic        = all(stage.systematic for stage in stages),
		invertible        = all(stage.invertible for stage in stages),
		deterministic     = all(stage.deterministic for stage in stages),
		error_propagating = any(stage.error_propagating for stage in stages),
		authenticated     = any(stage.authenticated for stage in stages),
	)


def _compose_expansion(decl: ast.CodecDecl, stages: list[ast.CodecDecl]
		) -> tuple[ast.Expansion, tuple[int, int] | None, int]:
	"""The product of the stages', with the weakest form winning.

	Unbounded anywhere is unbounded overall. A bounded ratio anywhere makes the
	product bounded rather than exact, because a later exact stage multiplies a
	range and gets a range. Two exact ratios multiply.
	"""
	forms = [stage.expansion for stage in stages]

	if ast.Expansion.UNBOUNDED in forms:
		return ast.Expansion.UNBOUNDED, None, 0

	numerator   = 1
	denominator = 1
	added       = 0
	bounded     = False
	exact       = False

	for stage in stages:
		if stage.expansion is ast.Expansion.FIXED_ADD:
			# Parity a later stage will expand along with everything else.
			# `rs |> manchester` appends 32 bytes and then doubles all of it,
			# so the addend has to be carried forward and scaled by whatever
			# follows rather than added at the end.
			added += stage.expansion_add
			continue
		if stage.ratio is None:
			continue

		exact   = exact or stage.expansion is ast.Expansion.RATIO_EXACT
		bounded = bounded or stage.expansion is ast.Expansion.RATIO_BOUNDED
		numerator   *= stage.ratio[0]
		denominator *= stage.ratio[1]
		# Everything appended so far goes through this stage too.
		added = -(-added * stage.ratio[0] // stage.ratio[1])

	if not exact and not bounded:
		if added:
			return ast.Expansion.FIXED_ADD, None, added
		return ast.Expansion.PRESERVING, None, 0

	# The form stays the ratio even where an addend rides along: the form is
	# what decides whether interior positions stay computable, and appended
	# parity does not move the data in front of it
	# (doc/decision/0016-composed-expansion.md).
	form = (ast.Expansion.RATIO_BOUNDED if bounded
	        else ast.Expansion.RATIO_EXACT)
	return form, _reduce(numerator, denominator), added


def _reduce(numerator: int, denominator: int) -> tuple[int, int]:
	from math import gcd
	divisor = gcd(numerator, denominator) or 1
	return (numerator // divisor, denominator // divisor)


def _weakest_seekable(stages: list[ast.CodecDecl]) -> ast.Seekable:
	order = (ast.Seekable.LINEAR, ast.Seekable.PERMUTED, ast.Seekable.NONE)
	return max((stage.seekable for stage in stages), key=order.index)


def _coarsest_granularity(stages: list[ast.CodecDecl]) -> ast.Granularity:
	order = (ast.Granularity.BIT, ast.Granularity.SYMBOL, ast.Granularity.BYTE,
	         ast.Granularity.BLOCK, ast.Granularity.STREAM)
	return max((stage.granularity for stage in stages), key=order.index)


# ---------------------------------------------------------------------------
# Reading a kernel's arguments
# ---------------------------------------------------------------------------


def _positive(kernel: ast.Kernel, name: str, decl: ast.CodecDecl) -> int:
	value = kernel.argument(name)
	number = value.value if isinstance(value, ast.IntLiteral) else None

	if number is None or number <= 0:
		raise error(
			f"`{decl.name}` needs `{name}` in its "
			f"{kernel.family.value} kernel",
			kernel.span,
			label = f"expected `{name} = N` with a positive literal",
			notes = [f"a {kernel.family.value} kernel derives its properties "
			         f"from its arguments, and `{name}` is one of them"],
		)

	return number


def _name_of(expr: ast.Expr | None) -> str | None:
	return expr.name if isinstance(expr, ast.NameRef) else None


# ---------------------------------------------------------------------------
# Resolving a whole schema
# ---------------------------------------------------------------------------


def resolve_signatures(schema: ast.Schema) -> None:
	"""Fill in every derived and composed signature, in dependency order.

	Run once after parsing, before anything reads a signature. From that point
	on a tier-2 codec is indistinguishable from a tier-1 one, which is what
	section 26.12 means by no propagation rule changing: the lattice reads nine
	properties and never asks where they came from.
	"""
	by_name = {decl.name: decl for decl in schema.codecs()}

	for index, decl in enumerate(schema.decls):
		if not isinstance(decl, ast.CodecDecl) or decl.kernel is None:
			continue
		filled = apply_to(decl)
		schema.decls[index] = filled
		by_name[filled.name] = filled

	for index, decl in enumerate(schema.decls):
		if not isinstance(decl, ast.CodecDecl) or not decl.pipeline:
			continue
		stages = [_stage(decl, name, by_name) for name in decl.pipeline]
		composed = compose(decl, stages)
		schema.decls[index] = composed
		by_name[composed.name] = composed


def _stage(decl: ast.CodecDecl, name: str,
		by_name: dict[str, ast.CodecDecl]) -> ast.CodecDecl:
	stage = by_name.get(name)
	if stage is None:
		known = ", ".join(f"`{other}`" for other in sorted(by_name)) or "none"
		raise error(
			f"`{decl.name}` names an unknown stage `{name}`",
			decl.span,
			label = "not a declared codec",
			notes = [f"codecs in this schema: {known}"],
		)

	if stage.pipeline:
		raise error(
			f"`{decl.name}` names the pipeline `{name}` as a stage",
			decl.span,
			label = "a stage must be a codec, not another pipeline",
			notes = ["nesting one pipeline in another composes the same "
			         "properties twice; write the stages out"],
		)

	return stage
