"""Cross-field invariants, held to the same meaning in all four backends.

Section 16.1's construct is small, and every one of its bugs so far has been a
disagreement rather than a crash: two backends numbering the same dirty bit
differently, one emitting a plain setter where the others refused, one
evaluating an expression the rest declined. None of those show up while a
backend is read on its own, which is why the tests that matter here compare
them.

`situc.invariant` holds the walk and each backend supplies the leaves, so the
question "which expressions are evaluable" has one answer by construction. The
tests below are what stops that from quietly becoming four answers again.
"""

from __future__ import annotations

from typing import Callable, Protocol

import pytest

from situc.codegen.c.emit import generate as generate_c
from situc.codegen.cpp.emit import generate as generate_cpp
from situc.codegen.python.emit import generate as generate_py
from situc.codegen.rust.emit import generate as generate_rs
from situc.layout import solve
from situc.parser import parse_text
from situc import ast
from situc.capability import Axis
from situc.resolve import ResolvedSchema, resolve
from situc.traverse import obligations

PREAMBLE = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: A tag and an invariant in one struct: the only shape where the two dirty-bit
#: numberings can disagree, and the shape nothing in the tree had.
BOTH = """struct s {
	u16 total;
	u8  a;
	u32 b;
	authenticated inner { u16 seq; }
	tag u8[4] covers(inner);
}
invariant s.total == size(s.a) + size(s.b);
"""

class Emitted(Protocol):
	"""What every backend's `generate` returns: files, whatever else it holds.

	Spelled out because the four return four different `Generated` types, and
	a bare dict of them infers as `object` -- which typechecks nothing.
	"""

	def files(self) -> dict[str, str]: ...


Emit = Callable[[ast.Schema, ResolvedSchema, str], Emitted]

#: What each backend calls the recompute for `s.total`, and how it spells the
#: bit. Four surfaces over one construct.
BACKENDS: dict[str, tuple[Emit, str]] = {
	"c":      (generate_c,   "void situ_s_total_recompute("),
	"cpp":    (generate_cpp, "void recompute_total("),
	"python": (generate_py,  "def recompute_total(self)"),
	"rust":   (generate_rs,  "pub fn recompute_total(&mut self"),
}


def sources(body: str) -> dict[str, str]:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return {name: "\n".join(emit(schema, resolved, "unit").files().values())
	        for name, (emit, _) in BACKENDS.items()}


# -- what every backend must do ---------------------------------------------


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_emits_a_recompute(target: str) -> None:
	"""Without it a schema can state a relationship and never satisfy it.

	C++, Python and Rust inherited only the *refusal* to write the field --
	`mutate = Immutable`, which the lattice hands them for free -- so for a
	while three of the four had a derived field nothing could set.
	"""
	assert BACKENDS[target][1] in sources(BOTH)[target]


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_emits_a_plain_setter_for_a_derived_field(target: str) -> None:
	"""An invariant decides the value; writing it directly would make the
	schema's own statement false."""
	source = sources(BOTH)[target]

	assert "situ_s_total_set" not in source
	assert "def total(self, value" not in source		# no property setter
	assert "void set_total(" not in source
	assert "pub fn set_total(" not in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_covered_write_is_not_a_plain_assignment(target: str) -> None:
	"""It leaves something stale, so it takes whatever holds the dirty word.

	C++ emitted `set_a(value)` and marked nothing, which made the map's
	`auth = Covered(...)` untrue in that language alone. Rust refused the
	setter outright, which is sound and too narrow -- the same schema then
	meant something smaller there than in C.
	"""
	source = sources(BOTH)[target]

	assert "void set_a(std::uint8_t value)" not in source
	assert "pub fn set_a(&mut self, value:" not in source
	assert "No set_a()" not in source


# -- the numbering ----------------------------------------------------------


def test_the_dirty_bits_are_the_same_in_every_backend() -> None:
	"""A caller reading a bit out of one language's generated code and checking
	it against another's must find the same answer. C numbered invariants after
	its own list of tags; Python numbered from a list of tags alone and fell
	back to bit 0, which is the first tag's."""
	source = sources(BOTH)

	# The tag is bit 0 and the invariant is bit 1, spelled four ways.
	assert "#define SITU_S_TAG_DIRTY 0x1u"   in source["c"]
	assert "#define SITU_S_TOTAL_STALE 0x2u" in source["c"]
	assert "dirty_tag = 0x1u"                in source["cpp"]
	assert "dirty_total = 0x2u"              in source["cpp"]
	assert "DIRTY_TAG: u32 = 0x1"            in source["rust"]
	assert "DIRTY_TOTAL: u32 = 0x2"          in source["rust"]
	assert "DIRTY_TAG = 0x1"                 in source["python"]
	assert "DIRTY_TOTAL = 0x2"               in source["python"]
	# Named at the call sites too. Python wrote the literal there, so a reader
	# comparing `mark_dirty(2)` here against `DIRTY_TOTAL` elsewhere had to
	# work out that they were the same bit.
	assert "mark_dirty(self.DIRTY_TOTAL)"  in source["python"]
	assert "clear_dirty(self.DIRTY_TOTAL)" in source["python"]


def test_tags_keep_their_bits_when_an_invariant_is_added() -> None:
	"""Renumbering them would change what an already-generated header means to
	a caller who stored one, and there is no version of this worth that."""
	without = sources("""struct s {
		u16 total;
		u8  a;
		authenticated inner { u16 seq; }
		tag u8[4] covers(inner);
	}
	""")
	with_it = sources(BOTH)

	assert "#define SITU_S_TAG_DIRTY 0x1u" in without["c"]
	assert "#define SITU_S_TAG_DIRTY 0x1u" in with_it["c"]


# -- what every backend must refuse -----------------------------------------

#: Right-hand sides no backend may evaluate. Each is refused for a reason the
#: language gives, not because it is awkward to emit -- so a backend that got
#: clever about one of these would be the one making the schema mean two
#: things.
REFUSED = [
	# Not arithmetic. An invariant says a field *equals* something; the moment
	# the right side can be a predicate it is a second `require`.
	"invariant s.total == size(s.a) > 1;",
	# Not a member. `size(a + b)` is nobody's size, and guessing which half was
	# meant would put a number in generated code that nobody wrote.
	"invariant s.total == size(s.a + s.b);",
	# `checksum(...)` is not on this list: it is refused by the front end
	# rather than declined by each backend, because "this build cannot
	# evaluate it" is a true thing to say about a dynamic offset and a
	# misleading thing to say about a question that does not exist.
]


@pytest.mark.parametrize("line", REFUSED)
@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_evaluates_what_the_others_refuse(target: str,
		line: str) -> None:
	body = "struct s {\n\tu16 total;\n\tu8 a;\n\tu32 b;\n}\n" + line + "\n"

	source = sources(body)[target]

	assert BACKENDS[target][1] not in source, (
		f"{target} evaluated `{line}`, which the others decline")


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_unevaluable_right_side_says_so(target: str) -> None:
	"""The refusal to write the field still stands, so the invariant cannot be
	broken -- only left unsatisfiable. Generated code says which, rather than
	leaving a reader to notice a function that is not there."""
	body = ("struct s {\n\tu16 total;\n\tu8 a;\n\tu32 b;\n}\n"
	        "invariant s.total == size(s.a + s.b);\n")

	source = sources(body)[target]

	assert "cannot be broken -- only left unsatisfiable" in source


# -- the obligation model ---------------------------------------------------


def test_an_invariant_covers_what_its_right_side_reads() -> None:
	"""Which is what makes a write to `a` leave `total` stale. The coverage
	machinery was already there for tags; question 3's answer was that this
	needed no more than to reuse it."""
	schema   = parse_text(PREAMBLE + BOTH)
	resolved = resolve(schema, solve(schema))
	struct   = resolved.structs["s"]

	covered = {entry.placement.path: entry.placement.covered_by
	           for entry in struct.entries}

	assert covered["s.a"] == ("invariant total",)
	assert covered["s.b"] == ("invariant total",)
	assert obligations(schema, struct)[1].kind == "invariant"


def test_the_derived_field_is_immutable_and_says_which_invariant_decided() -> None:
	schema   = parse_text(PREAMBLE + BOTH)
	resolved = resolve(schema, solve(schema))
	struct   = resolved.structs["s"]

	total = next(entry for entry in struct.entries
	             if entry.placement.path == "s.total")

	assert total.vector.get(Axis.MUTATE).base == "Immutable"
	assert total.placement.derived_by == "invariant total"


# -- delimited members, where a backend does not have them yet ---------------


DELIMITED = 'struct s { u8 line[] until "\\r\\n"; u16 count; }'


@pytest.mark.parametrize("target,marker", [
	("c", "situ_s_line_span"),
	("cpp", "line_span()"),
	("python", "def line_span(self)"),
	("rust", "pub fn line_span(&self)"),
])
def test_every_backend_has_delimited_members(target: str, marker: str) -> None:
	"""All four, which is what closes section 8.6.

	This replaces a pair of tests that said three did not: one asserting the
	refusal and one asserting C did have it, written together so that the
	first failed the day a backend caught up. It did, three times, which is
	the whole reason to write the second half."""
	schema   = parse_text(PREAMBLE + DELIMITED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	assert marker in "\n".join(emit(schema, resolved, "unit").files().values())


# -- one spelling per value (section 8.6.2) ---------------------------------

MINIMAL = 'struct s { decimal u16 n until "\\r\\n" [minimal]; u8 r[remaining]; }'
LOOSE   = 'struct s { decimal u16 n until "\\r\\n"; u8 r[remaining]; }'


def test_a_text_number_is_non_canonical_by_default() -> None:
	"""`007` and `7` are one value written two ways. Decision 0020 argued the
	`canonical` axis has more to say about text than about binary, and then the
	first text construct shipped without using it."""
	schema   = parse_text(PREAMBLE + LOOSE)
	resolved = resolve(schema, solve(schema))
	entry    = next(e for e in resolved.structs["s"].entries
	                if e.placement.path == "s.n")

	assert entry.vector.get(Axis.CANONICAL).base == "NonCanonical"


def test_minimal_buys_the_single_spelling_back() -> None:
	schema   = parse_text(PREAMBLE + MINIMAL)
	resolved = resolve(schema, solve(schema))
	entry    = next(e for e in resolved.structs["s"].entries
	                if e.placement.path == "s.n")

	assert entry.vector.get(Axis.CANONICAL).base == "Canonical"


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_enforces_minimal(target: str) -> None:
	"""A capability claim nothing backs is worse than no claim. `Canonical`
	here is a promise that the bytes follow from the value, and only the
	generated check makes it true."""
	schema   = parse_text(PREAMBLE + MINIMAL)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "digits_minimal" in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_enforces_it_uninvited(target: str) -> None:
	"""Most formats do permit `007`, so refusing it unasked would reject valid
	data. The map reports NonCanonical instead, which is the honest default."""
	schema   = parse_text(PREAMBLE + LOOSE)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "digits_minimal" not in source


# -- optional whitespace and case-insensitive tokens (section 8.6.4) --------

MESSY = (
	'struct hdr {\n'
	'\tu8 name[]  until ":"    [case_insensitive];\n'
	'\tu8 value[] until "\\r\\n" [trim];\n'
	'}\n'
	'struct req { hdr fields[] until "\\r\\n"; u8 body[remaining]; }\n'
)


@pytest.mark.parametrize("field", ["hdr.name", "hdr.value"])
def test_both_cost_canonicity(field: str) -> None:
	"""`Content-Length` and `content-length` are one token with two spellings;
	` 5` and `5` are one value with two. That is what the axis is for, and both
	attributes are ways of saying the bytes do not follow from the value."""
	schema   = parse_text(PREAMBLE + MESSY)
	resolved = resolve(schema, solve(schema))
	entry    = next(e for s in resolved.structs.values() for e in s.entries
	                if e.placement.path == field)

	assert entry.vector.get(Axis.CANONICAL).base == "NonCanonical"


def test_trimming_does_not_move_the_next_member() -> None:
	"""`[trim]` changes what the value *is*, not where the member ends. The
	whitespace is still this member's bytes -- members partition their struct
	exactly -- so the framing is untouched and only the value is narrower."""
	schema   = parse_text(PREAMBLE + MESSY)
	resolved = resolve(schema, solve(schema))

	value = next(e for s in resolved.structs.values() for e in s.entries
	             if e.placement.path == "hdr.value")

	assert value.placement.trimmed
	# The span the layout solver computed is the delimiter run, not the
	# trimmed content: trimming is an accessor-level fact.
	assert value.placement.size_bits == 2 * 8


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_trims_and_folds(target: str) -> None:
	schema   = parse_text(PREAMBLE + MESSY)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "trim" in source
	assert "ci_eq" in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_plain_token_compares_byte_for_byte(target: str) -> None:
	"""The comparison is generated either way, because "is this field
	`Content-Length`" is what a caller of a text format actually asks. What
	changes is whether case is folded, and the schema decides that -- not each
	caller separately."""
	plain    = 'struct s { u8 name[] until ":"; u8 rest[remaining]; }'
	schema   = parse_text(PREAMBLE + plain)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "name_eq" in source
	assert "ci_eq" not in source


# -- versioned members (section 19.4) ---------------------------------------

VERSIONED = ("struct s [version = v] { u8 v; u16 a; u32 b [since = 2]; }")


#: How each backend spells the refusal. Written out rather than matched
#: loosely: a substring search for "version" passes on the doc comment that
#: says which version a member arrives in, which is exactly the text an
#: ungated getter would still carry.
VERSION_REFUSAL = {
	"c":      "return SITU_ERR_VERSION;",
	"cpp":    "return ::situ::rt::err::version;",
	"python": "raise VersionError(",
	"rust":   "return Err(Error::Version);",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_reads_a_versioned_field_unguarded(target: str) -> None:
	"""The three that were not C emitted a plain getter and built in silence,
	which is worse than crashing: a caller reading `b` from a v1 message got
	whatever followed in the buffer, with nothing to check."""
	schema   = parse_text(PREAMBLE + VERSIONED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert VERSION_REFUSAL[target] in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_writes_one_unguarded_either(target: str) -> None:
	"""The asymmetry every backend had, C included: the getter refused from
	the start and the setter did not. Reading the wrong bytes is a wrong
	answer; writing them is somebody else's data."""
	schema   = parse_text(PREAMBLE + VERSIONED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "void situ_s_b_set(situ_view_t view" not in source
	assert "void set_b(std::uint32_t value)" not in source
	assert "pub fn set_b(&mut self, value: u32) {" not in source


# -- a nested struct with no single size ------------------------------------

NESTED = (
	"struct inner { u8 n; u8 body[n]; }\n"
	"struct outer { inner a; u16 tail; }\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_sizes_a_nested_variable_struct(target: str) -> None:
	"""Each got this wrong in its own way, and the flavours are instructive.

	C and C++ named a `SIZE_FIXED`/`size_bytes` constant that is emitted only
	where a struct has one size, so neither compiled. Python named
	`SIZE_BYTES` and raised `AttributeError` at the point of use, which is the
	worst place for it to arrive. Rust handed the member everything to the end
	of the buffer -- which its own accessors survive -- and then dropped the
	member *after* it from the module entirely, because there was no extent to
	add to its offset.
	"""
	schema   = parse_text(PREAMBLE + NESTED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	# The generated helper by name, not the word: "extent" also appears in the
	# acquisition docstring every backend emits, so a bare substring passes on
	# a schema that has none of this (invariant 26).
	assert "a_extent" in source
	for absent in (
		"SITU_INNER_SIZE_FIXED", "inner::size_bytes", "inner.SIZE_BYTES",
	):
		assert absent not in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_the_member_after_one_is_still_placed(target: str) -> None:
	"""The half that is silent rather than loud. A nested variable struct
	contributed nothing to the offset sum, so whatever followed it was placed
	on top of it -- or, in Rust, left out."""
	schema   = parse_text(PREAMBLE + NESTED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "tail" in source
	assert "cannot be resolved" not in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_nested_fixed_struct_still_uses_its_constant(target: str) -> None:
	"""The common case, and the one that would be a pointless cost to lose."""
	schema   = parse_text(PREAMBLE + "struct inner { u16 x; }\n"
	                      "struct outer { inner a; u16 tail; }\n")
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "a_extent" not in source


# -- runs ending on a condition (section 8.6.6) -----------------------------

WHILE_RUN = (
	"struct e { u8 next; u8 len; u8 d[(len + 1) * 8 - 2]; }\n"
	"struct s { e chain[] while (next == 43 || next == 44); u8 rest[remaining]; }\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_walks_a_while_run(target: str) -> None:
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "chain_count" in source
	assert "has not caught up" not in source


#: How each backend spells reading `len` inside `(len + 1) * 8 - 2`. Written
#: out rather than matched loosely, because the loose version passed on Rust
#: while Rust emitted a one-byte scalar (invariant 26, again).
SIZED_BY_EXPRESSION = {
	"c":      "(situ_e_len_get(view) + 1) * 8 - 2",
	"cpp":    "(len() + 1) * 8 - 2",
	"python": "(self.len + 1) * 8 - 2",
	"rust":   "((self.len() as usize) + 1) * 8 - 2",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_reads_an_expression_sized_member_as_a_scalar(
		target: str) -> None:
	"""`sized_by` holds a field path and holds nothing for arithmetic over
	one, so `traverse.classify` called `d[(len + 1) * 8 - 2]` a SCALAR and
	three backends handed back one byte and called it the field. C escaped
	only because it does not use the classifier -- invariant 20 pointing the
	other way for once."""
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert SIZED_BY_EXPRESSION[target] in source


def test_the_python_condition_is_python() -> None:
	"""The schema's operators are C's. Python spells three of them in words,
	and emitting `||` produced a module that did not parse."""
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS["python"]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	# The condition as *code*. `||` legitimately survives in the docstring
	# that quotes the schema, which is what a looser assertion caught.
	assert "if not (self.next == 43 or self.next == 44)" not in source
	assert "if not (element.next == 43 or element.next == 44):" in source


def test_the_rust_reads_are_widened() -> None:
	"""`(len + 1) * 8` in u8 arithmetic is 255 + 1 = 0, then zero. C computes
	it correctly only because integer promotion widens to `int` first, which
	is a rule Rust does not have -- and a guarantee C stops giving above 16
	bits."""
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS["rust"]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "as usize) + 1" in source
