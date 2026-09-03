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

from every_schema import ROOT
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


def from_disk(relative: str) -> dict[str, str]:
	"""Every backend's output for a committed schema.

	`sources` builds one from a fragment; these two tests need `example/mqtt`
	and `example/netlink` as they stand, because the arms they are about are
	what those protocols actually declare.
	"""
	path     = ROOT / relative
	schema   = parse_text(path.read_text(encoding="ascii"))
	resolved = resolve(schema, solve(schema))
	return {name: "\n".join(emit(schema, resolved, "unit").files().values())
	        for name, (emit, _) in BACKENDS.items()}



#: A variant arm whose type is a struct the backend cannot measure, and the
#: name every backend gives the offset accessor for it. C spells it
#: `situ_packet_body_publish_offset` and the other three
#: `body_publish_offset`, so the shorter one is a substring of all four.
ARM_OFFSETS = (
	("example/mqtt/mqtt.situ",       "body_publish_offset"),
	("example/mqtt/mqtt.situ",       "body_subscribe_offset"),
	("example/mqtt/mqtt.situ",       "body_suback_offset"),
	("example/mqtt/mqtt.situ",       "body_unsubscribe_offset"),
)


@pytest.mark.parametrize("schema,accessor", ARM_OFFSETS,
                         ids=[a for _, a in ARM_OFFSETS])
def test_every_backend_offers_an_unmeasurable_arms_offset(
		schema: str, accessor: str) -> None:
	"""The offset is not the extent, and the four used to disagree about it.

	C emitted `situ_packet_body_publish_offset` and the other three emitted
	nothing; 26.190 recorded that as a divergence it could not resolve --
	"either C's offset accessor is a capability the other three should have,
	or it is one C should not offer". The capability map answers it:
	`packet.body.publish` is `offset=Dynamic`, so it has an offset the
	message decides, and `packet.body.publish.topic_length` is
	`FrameStatic(0x00)` from there. A caller holding the offset can reach the
	interior; a caller holding nothing cannot.

	Asserted here rather than left to
	`test_the_backends_refuse_the_same_members`, which cannot see it: that
	file scores a *refusal* by finding a phrase near a member's path, so a
	member nobody refuses and nobody serves scores the same as one all four
	serve. Dropping the accessor from Python or Rust leaves all 42 of its
	cases green.
	"""
	for backend, text in sorted(from_disk(schema).items()):
		assert accessor in text, (
			f"{backend} offers no offset for this arm; C has emitted one "
			f"since before 26.190 and the map says the member has one")


def test_an_opaque_arm_is_declined_by_all_four() -> None:
	"""The other half of the same question, and the one that stops the fix
	above from trading one divergence for another.

	`netlink`'s default arm is `opaque rest[nlmsg_len - 16]` at a *static*
	offset, and C emits nothing for it -- not the offset, not the bytes.
	26.190's exemption said C emitted an offset accessor for this member as
	it does for mqtt's four; it does not, and that half of the reason was
	wrong. All four decline it together, which is agreement and needs no
	exemption at all.
	"""
	for backend, text in sorted(
			from_disk("example/netlink/netlink.situ").items()):
		assert "body_rest_offset" not in text, (
			f"{backend} offers an offset for an opaque arm that C does not")


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


#: A predicate situ and two of its four host languages read differently when
#: it is written flat. situ's precedence is C's, so `&` binds looser than
#: `==` and this is `kind & (3 == 2)` -- which is `kind & 0`, and false for
#: every `kind`. Python and Rust bind `&` tighter, so the same characters are
#: `(kind & 3) == 2` there, which is true whenever the low two bits are 2.
#: Both text-carrying fields, because they are separate code paths: a `while`
#: predicate and a computed size. `n | 1 == 1` is `n | (1 == 1)` here and
#: `(n | 1) == 1` in Python, and both readings are lengths a schema may have,
#: so the compiler accepts it either way and only the grouping decides which.
REGROUPABLE = ("struct e { u8 kind; u8 len [max = 8]; u8 body[len]; }\n"
               "struct s { u8 n [max = 32]; u8 pad[n | 1 == 1];"
               " e items[] while (kind & 3 == 2) max 6; }\n")


def test_a_hosts_precedence_cannot_regroup_an_expression() -> None:
	"""A `while` predicate and a computed size reach a host compiler as
	*text*, and the host reparses it under its own table.

	situ's table is C's. Python's and Rust's are not, where a bitwise
	operator meets a comparison; Python adds a second divergence by chaining
	comparisons, so `a > b < c` is `(a > b) and (b < c)` and not
	`(a > b) < c`. Comparing situ's grouping against Python's reading of the
	same flat text finds 39 operator pairs that disagree.

	No committed schema writes one, which is why nothing had noticed -- the
	same sample-size argument 26.37 made about this language's operators, one
	level down. The expression is parenthesised at every nested operator now,
	which is correct in any language that has brackets and costs a few
	characters in generated code nobody edits.
	"""
	# The premise, proved here rather than asserted: the flat text means
	# something else in Python. If this ever stops being true the test below
	# is checking nothing, and it should be deleted rather than kept green.
	assert eval("2 & 3 == 2") != eval("2 & (3 == 2)")

	schema   = parse_text(PREAMBLE + REGROUPABLE)
	resolved = resolve(schema, solve(schema))

	for target, (emit, _) in sorted(BACKENDS.items()):
		source = "\n".join(emit(schema, resolved, "unit").files().values())
		assert "(3 == 2)" in source, (
			f"{target}: the predicate's comparison is not grouped, so the "
			f"host decides what `kind & 3 == 2` means")
		assert "& 3 == 2" not in source, (
			f"{target}: emitted the flat predicate, which Python and Rust "
			f"read as `(kind & 3) == 2` and situ reads as `kind & (3 == 2)`")
		assert "(1 == 1)" in source, (
			f"{target}: the *size* expression is not grouped; it is a second "
			f"path and reverting it alone left this test green")
		assert "| 1 == 1" not in source, f"{target}: flat size expression"


#: How each backend spells reading `len` inside `(len + 1) * 8 - 2`. Written
#: out rather than matched loosely, because the loose version passed on Rust
#: while Rust emitted a one-byte scalar (invariant 26, again).
#:
#: Parenthesised at every nested operator: the expression reaches a host
#: compiler as text, and Python's and Rust's precedence tables are not situ's
#: where a bitwise operator meets a comparison. The grouping written here is
#: the one situ always meant; the brackets are what stop it depending on who
#: reads it. The outermost pair is absent because Rust's `-D unused-parens`
#: refuses it.
SIZED_BY_EXPRESSION = {
	"c":      "((situ_leaf_u64(situ_e_len_get(view)) + 1) * 8) - 2",
	"cpp":    "((::situ::rt::leaf_u(len()) + 1) * 8) - 2",
	"python": "((leaf(self.len) + 1) * 8) - 2",
	"rust":   "((situ_rt::leaf_u(self.len() as u64) + 1) * 8) - 2",
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


#: The same question asked of an `opaque` region: how many bytes is it, when
#: its size is arithmetic rather than a bare field?
SIZED_OPAQUE = "struct s { u8 n; opaque body[n + 1]; u16 tail; }\n"

SIZED_OPAQUE_LENGTH = {
	"c":      "situ_nonneg_u32(situ_leaf_u64(situ_s_n_get(view)) + 1)",
	"cpp":    "::situ::rt::nonneg(::situ::rt::leaf_u(n()) + 1)",
	"python": "advance(1, nonneg(leaf(self.n) + 1), self._len)",
	"rust":   "situ_rt::nonneg(situ_rt::leaf_u(self.n() as u64) + 1)",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_no_backend_reads_a_sized_opaque_region_as_nothing(target: str) -> None:
	"""An `opaque` region records `sized_by`, which holds a path and holds
	nothing for `[n + 1]`. `size_expr` is the field that answers this for an
	array and `place_opaque` never set it, so all four backends computed the
	region's length as zero and placed `tail` one byte in -- reading the
	region's own bytes and calling them the field.

	The same defect as `d[(len + 1) * 8 - 2]` above, found and fixed for
	arrays and never asked of the construct beside them (invariant 1)."""
	schema   = parse_text(PREAMBLE + SIZED_OPAQUE)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert SIZED_OPAQUE_LENGTH[target] in source


#: `u32 d[n + 1]`: an arithmetic count over elements wider than a byte, which
#: is the case that separates "how many" from "how far".
WIDE_ARITHMETIC = "struct s { u8 n; u32 d[n + 1]; u16 tail; }\n"

WIDE_ARITHMETIC_LENGTH = {
	"c":      "situ_nonneg_u32((situ_leaf_u64(situ_s_n_get(view)) + 1) * 4)",
	"cpp":    "::situ::rt::nonneg((::situ::rt::leaf_u(n()) + 1) * 4)",
	"python": "nonneg((leaf(self.n) + 1) * 4)",
	"rust":   "situ_rt::nonneg((situ_rt::leaf_u(self.n() as u64) + 1) * 4)",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_every_backend_counts_elements_in_an_arithmetic_bracket(
		target: str) -> None:
	"""The bracket counts elements. `sized_by` renders as `count * width` in
	all four backends and `size_expr` rendered as bare bytes in all four, so
	the accessors disagreed with the solver -- which sizes `u32 d[n + 1]` at
	`(n + 1) * 4` -- by a factor of the element width.

	The shape that found `size_expr` was `u8 d[(len + 1) * 8 - 2]`, where that
	factor is one, so the bug arrived with its own reason for staying hidden.
	"""
	schema   = parse_text(PREAMBLE + WIDE_ARITHMETIC)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert WIDE_ARITHMETIC_LENGTH[target] in source


def test_the_solver_and_the_accessors_size_an_array_alike() -> None:
	"""The fact the four spellings are checked against: 256 elements of four
	bytes, which is where the disagreement was."""
	schema   = parse_text(PREAMBLE + WIDE_ARITHMETIC)
	resolved = resolve(schema, solve(schema))
	held     = {entry.placement.path: entry.placement
	            for entry in resolved.structs["s"].entries}

	assert held["s.d"].size_max_bits == 256 * 32
	assert held["s.d"].element_bits == 32


#: A packed pair before a member the data sizes. `u4` and `u4` are one byte
#: together and zero apart, which is the arithmetic `extent_parts` states in
#: its own docstring -- and which the offset accumulation beside it did per
#: member, in all four backends.
PACKED_BEFORE_DYNAMIC = (
	"struct s { u4 hi; u4 n; u32 d[n]; u16 tail; }\n"
)

PACKED_BASE = {
	"c":      "uint32_t offset = 1u;",
	"cpp":    "situ_advance_u32(1, ",
	"python": "advance(1, ",
	"rust":   "situ_rt::advance(1, ",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_packed_pair_before_a_dynamic_member_is_one_byte(target: str) -> None:
	"""`tail` sits after `d`, whose offset is the sum of what precedes it.
	That sum divided each member by eight and added the quotients, so two
	nibbles contributed nothing and every accessor after the run read one byte
	early -- the last byte of the array and the first of `tail`.

	`extent_parts` says this in its docstring, having been fixed for the
	*extent*; the offset sum next to it was left as it was (invariant 65)."""
	schema   = parse_text(PREAMBLE + PACKED_BEFORE_DYNAMIC)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert PACKED_BASE[target] in source
	assert "advance(0, " not in source and "offset = 0u;" not in source


#: A varint driving a size written as arithmetic. The count form `d[n]` has
#: known about varint drivers since they were added; the form beside it left
#: the name as a bare identifier.
VARINT_ARITHMETIC = (
	"varint_type v { encoding = be128; max_bits = 64; max_bytes = 9; }\n"
	"struct s { v n; u16 d[n + 1]; u16 tail; }\n"
)

VARINT_ARITHMETIC_READ = {
	"c":      "situ_leaf_u64(situ_s_n_value(view)) + 1",
	"cpp":    "::situ::rt::leaf_u(n_value()) + 1",
	"python": "leaf(self.n_value) + 1",
	"rust":   "situ_rt::leaf_u(self.n_value() as u64) + 1",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_expression_may_name_a_varint(target: str) -> None:
	"""`readable_names` is the one list of what an expression may name, and it
	asked for a scalar -- which a varint has not. So `n` reached the generated
	code verbatim: an undefined identifier in C, C++ and Rust, and a
	`NameError` in Python.

	`_value` rather than the plain getter, because an offset sum cannot report
	a truncated encoding -- which is the same choice the count form made."""
	schema   = parse_text(PREAMBLE + VARINT_ARITHMETIC)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert VARINT_ARITHMETIC_READ[target] in source


#: A `while` run whose element is a *fixed-size* struct. Every run in this
#: repository walks a variable one, which is the case `extent_parts` was
#: written for.
FIXED_ELEMENT_RUN = (
	"struct e { u8 k; u8 pad; }\n"
	"struct s { e c[] while (k == 1) max 4; u16 tail; }\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_run_may_walk_a_fixed_size_element(target: str) -> None:
	"""`extent_parts` returns None for a fixed struct, which is right --
	its extent is its size and callers have that already. Three backends read
	the None as "cannot measure" and emitted no walk at all, then went on
	emitting the members after the run, whose offsets call the span function
	that was not written. Rust and C++ do not compile; Python raises.

	C computes it from the size directly and was the only one that built such
	a schema, which is invariant 20 pointing the other way again."""
	schema   = parse_text(PREAMBLE + FIXED_ELEMENT_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "c_span" in source
	assert "has no extent" not in source


COUNTED_RECORD_RUN = (
	"struct e { u8 k; u8 body[k]; }\n"
	"struct s { u8 c; e recs[c]; u16 tail; }\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_counted_run_of_variable_records_has_a_span(target: str) -> None:
	"""`T x[n]` where `T` has no single size: there is no stride to multiply,
	so the run is walked and how far it reaches is the sum of the walk.

	Three backends emitted the indexing walk and no span, and declined every
	member after the run for want of one. C emitted neither and fell through
	to the counted-array branch, which multiplies the count by an element
	width -- so a run of `n` variable records measured `n` bytes and whatever
	followed it read the middle of the run."""
	schema   = parse_text(PREAMBLE + COUNTED_RECORD_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "recs_span" in source
	assert "No accessor for `tail`" not in source


def test_a_sized_opaque_region_moves_what_follows_it() -> None:
	"""And the fact under the four spellings: `tail` is not at a constant
	offset, and its offset is the region's size rather than zero."""
	schema   = parse_text(PREAMBLE + SIZED_OPAQUE)
	resolved = resolve(schema, solve(schema))
	held     = {entry.placement.path: entry.placement
	            for entry in resolved.structs["s"].entries}

	assert held["s.body"].size_expr == "n + 1"
	assert held["s.tail"].offset_bits is None		# the data decides it


def test_the_python_condition_is_python() -> None:
	"""The schema's operators are C's. Python spells three of them in words,
	and emitting `||` produced a module that did not parse."""
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS["python"]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	# The condition as *code*. `||` legitimately survives in the docstring
	# that quotes the schema, which is what a looser assertion caught.
	assert "if not ((self.next == 43) or (self.next == 44))" not in source
	assert "if not ((element.next == 43) or (element.next == 44)):" in source


def test_the_rust_reads_are_widened() -> None:
	"""`(len + 1) * 8` in u8 arithmetic is 255 + 1 = 0, then zero. C computes
	it correctly only because integer promotion widens to `int` first, which
	is a rule Rust does not have -- and a guarantee C stops giving above 16
	bits.

	`as u64` rather than `as usize` in a *size* expression, and the domain is
	the point rather than the width (14.2b): the leaf is bounded in the domain
	it was read in, because `as i64` on a `u64` above 2^63 makes a huge length
	indistinguishable from a negative one -- which then clamps to zero and
	puts the member after it inside the frame instead of past it.
	"""
	schema   = parse_text(PREAMBLE + WHILE_RUN)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS["rust"]

	source = "\n".join(emit(schema, resolved, "unit").files().values())

	assert "as u64) + 1" in source
	# Still widened, which is what this test has always been about: the read
	# must not stay in the field's own narrow type.
	assert "self.len() + 1" not in source


# -- a covered field of a nested struct (26.35) -----------------------------

NESTED_COVERED = """struct inner { u8 a; u16 b; }
struct s {
	u8  outside;
	authenticated { u8 direct; inner nested; }
	tag u8[16];
}
"""

#: What each backend calls the covered write for `s.nested.a`. The setter is
#: on the *parent*, because marking the bit needs the message and a nested
#: type cannot know a tag covers it: `inner` may sit where nothing does.
NESTED_SETTERS = {
	"c":      "situ_s_nested_a_set(situ_msg_t *msg",
	"cpp":    "void set_nested_a(::situ::rt::message &owner",
	"python": "def set_nested_a(self, msg: Message",
	"rust":   "pub fn set_nested_a(&mut self, dirty: &mut Dirty",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_covered_field_of_a_nested_struct_can_be_written(target: str) -> None:
	"""C flattened these onto the parent from the start; the other three
	emitted nothing, because `own_entries` drops a dotted path and a nested
	struct's fields have one.

	What that left was worse than a missing accessor: the nested type's own
	setter still writes the byte and marks nothing, so a caller could write a
	tag-covered field and have the message go on reporting itself
	transmittable -- while the map said `auth = Covered(tag)` about it. Found
	by making the differential drivers write (26.35)."""
	assert NESTED_SETTERS[target] in sources(NESTED_COVERED)[target]


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_that_write_marks_the_tag(target: str) -> None:
	"""The claim itself: 14.2 says a covered write leaves the tag stale, and
	a setter that writes without marking is the map lying."""
	source = sources(NESTED_COVERED)[target]
	marked = {
		"c":      "situ_msg_mark_dirty(msg, SITU_S_TAG_DIRTY)",
		"cpp":    "owner.mark_dirty(dirty_tag)",
		"python": "msg.mark_dirty(self.DIRTY_TAG)",
		"rust":   "dirty.mark(Self::DIRTY_TAG)",
	}[target]

	body = source[source.index(NESTED_SETTERS[target]):]
	assert marked in body[:400]


#: A run of values the message counts, whose elements are wider than a byte.
#: `u16 x[4]` is the same array with its count in the schema, and every backend
#: refuses to hand back its bytes -- the element is ValueConverted, so the bytes
#: are not the values. With the count in the message all four answered
#: differently.
WIDE_RUN = "struct s { u8 n; u16 a[n]; i32 b[n + 1]; u16 tail; }\n"

#: What indexing one looks like in each language. Spelled out rather than
#: grepped for a substring, because the shape that was wrong -- a span of the
#: raw bytes -- contains the member's name too (invariant 26).
WIDE_RUN_INDEXED = {
	"c":      "situ_s_a_get(situ_view_t view, uint32_t index)",
	"cpp":    "std::uint16_t a(std::uint32_t index) const noexcept",
	"python": "def a(self, index: int) -> int:",
	"rust":   "pub fn a(&self, index: usize) -> Result<u16> {",
}

#: And the byte spellings each of them used to emit instead.
WIDE_RUN_AS_BYTES = {
	"c":      "situ_s_a_ptr(",
	"cpp":    "::situ::rt::bytes a() const noexcept",
	"python": "def a(self) -> memoryview:",
	"rust":   "pub fn a(&self) -> &[u8] {",
}


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_run_the_message_counts_is_read_by_index(target: str) -> None:
	"""Three backends handed back the raw bytes of a `u16 x[n]`, each of them
	three lines under a comment saying why the constant-count form does not:
	the element is ValueConverted, so a caller casting the span reads host
	byte order for a schema that names its own."""
	source = sources(WIDE_RUN)[target]

	assert WIDE_RUN_INDEXED[target] in source
	assert WIDE_RUN_AS_BYTES[target] not in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_arithmetic_count_of_wide_elements_is_an_array(target: str) -> None:
	"""The same array, its count written as arithmetic. In C that spelling
	reached the *scalar* branch and got a getter taking no index -- so the
	array had one element as far as any caller could tell -- and the other
	three handed back bytes as they did for the count form."""
	source = sources(WIDE_RUN)[target]
	indexed = {
		"c":      "situ_s_b_get(situ_view_t view, uint32_t index)",
		"cpp":    "std::int32_t b(std::uint32_t index) const noexcept",
		"python": "def b(self, index: int) -> int:",
		"rust":   "pub fn b(&self, index: usize) -> Result<i32> {",
	}[target]

	assert indexed in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_element_of_a_counted_run_is_bounded(target: str) -> None:
	"""Invariant 41, for the accessor that had it wrong: the count is the
	message's, so a caller looping to it is not making the mistake a fixed
	array's index would be. C read element 99 of an `n` of 200 from four
	hundred bytes past an eight-byte frame."""
	source = sources(WIDE_RUN)[target]
	bounded = {
		"c":      "situ_in_bounds(view, 1u + index * 2u, 2u)",
		"cpp":    "index < a_count() ?",
		"python": "if not 0 <= index < self.a_count:",
		"rust":   "if index >= self.a_count() {",
	}[target]

	assert bounded in source


#: A member whose byte order the message decides, behind a member whose length
#: it decides. `example/tiff` is the only marker in the tree and its fields
#: are all at constant offsets, so the marker-conditional load had never been
#: asked for a dynamic one.
MARKED_DYNAMIC = (
	"endian_marker mark : u16 { little = 0x4949, big = 0x4D4D }\n"
	"struct s [endian = from(mark)] {\n"
	"\tendian_marker  mark;\n"
	"\tu8             n;\n"
	"\tu8             pad[n];\n"
	"\tu16            after;\n"
	"}\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_marker_governs_a_member_at_a_dynamic_offset(target: str) -> None:
	"""C asked the placement for a constant offset while every other load on
	that path takes one from the caller, and crashed inside `offset_bytes` --
	an internal error where section 17 asks for a diagnostic.

	Rust emitted `self.as_ref().as_ref()` in the setter: the rewrite that
	moves a read onto the immutable view rewrote an `as_ref()` the store had
	already put on the marker predicate (invariant 53)."""
	source = sources(MARKED_DYNAMIC)[target]
	reads  = {
		"c":      "situ_s_mark_is_little(view) ? situ_get_le16",
		"cpp":    "mark_is_little() ? situ_get_le16",
		"python": "big=not self.mark_is_little",
		"rust":   "if self.mark_is_little() { situ_rt::read_le",
	}[target]

	assert reads in source
	assert "as_ref().as_ref()" not in source


#: A variant arm that is a run of values wider than a byte, counted by the
#: message and by the schema. An arm may be a scalar, a byte run or a struct,
#: and this was none of those in any backend.
ARM_RUN = (
	"enum k : u8 { one = 0x11, two = 0x22, default = error }\n"
	"struct s {\n"
	"\tk   which;\n"
	"\tu8  n;\n"
	"\tvariant body switch (which) {\n"
	"\t\tcase k.one: u16  wide[n];\n"
	"\t\tcase k.two: i16  pinned[3];\n"
	"\t}\n"
	"}\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_arm_may_be_a_run_of_wide_values(target: str) -> None:
	"""All four declined it: C and C++ said so in the generated file, Python
	and Rust said nothing at all. The count and the indexed getter are what an
	ordinary run gets, and the arm test goes in front of the count, which the
	getter reaches through."""
	source = sources(ARM_RUN)[target]
	indexed = {
		"c":      "situ_s_body_wide_get(situ_view_t view, uint32_t index,"
		          " uint16_t *out)",
		"cpp":    "err body_wide(std::uint32_t index, std::uint16_t &out)",
		"python": "def body_wide(self, index: int) -> int:",
		"rust":   "pub fn body_wide(&self, index: usize) -> Result<u16> {",
	}[target]

	assert indexed in source
	assert "not a shape this backend reaches into" not in source


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_an_arm_of_wide_values_counts_what_is_there(target: str) -> None:
	"""The count is the arm's own, clamped to the frame like every other one
	the message decides -- and the schema-counted spelling beside it was as
	unreachable as the message-counted one, an arm's base being dynamic
	whatever its count says."""
	source = sources(ARM_RUN)[target]
	counts = {
		"c":      "situ_s_body_pinned_count(situ_view_t view, uint32_t *out)",
		"cpp":    "err body_pinned_count(std::uint32_t &out)",
		"python": "def body_pinned_count(self) -> int:",
		"rust":   "pub fn body_pinned_count(&self) -> Result<usize> {",
	}[target]

	assert counts in source


#: A nested struct behind a variable-length member: a header, a variable-length
#: field, and another header. C emitted it from the start and the other three
#: asked the placement for a constant offset.
NESTED_DYNAMIC = (
	"struct inner { u16 seq; u8 flags; }\n"
	"struct s { u8 n; u8 pad[n]; inner head; u16 tail; }\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_nested_struct_may_sit_at_a_dynamic_offset(target: str) -> None:
	"""Three backends crashed the compiler on the assertion inside
	`offset_bytes` for this, which is as ordinary a shape as a protocol has.
	An internal error is where section 17 asks for a diagnostic -- and the
	shape is not one situ refuses, so there was nothing to diagnose."""
	source = sources(NESTED_DYNAMIC)[target]
	reaches = {
		"c":      "situ_s_head_view(situ_view_t view, situ_view_t *out)",
		"cpp":    "err head(::situ::inner &out)",
		"python": "def head(self) -> inner:",
		"rust":   "pub fn head(&self) -> Result<Inner<'_>> {",
	}[target]

	assert reaches in source
	assert "cannot resolve where it starts" not in source


#: The same nested struct, covered by a tag. Its fields get the flattened
#: setter that marks the bit, and that setter wrote at the parent's base plus a
#: *frame-relative* offset -- `head.seq` over the top of `n`.
COVERED_DYNAMIC = (
	"struct inner { u16 seq; u8 flags; }\n"
	"struct s {\n"
	"\tu8 n; u8 pad[n];\n"
	"\tauthenticated body { inner head; }\n"
	"\ttag u8 mac[4] covers(body);\n"
	"}\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_covered_nested_setter_finds_its_own_frame(target: str) -> None:
	"""`s.head.seq` is at offset 0 *of `head`*, and `head` is wherever `pad`
	leaves it. Every backend added the frame-relative offset to the parent's
	base: C wrote over `n`, and the other three crashed before they got far
	enough to write anything."""
	source = sources(COVERED_DYNAMIC)[target]
	composed = {
		"c":      "view.base + situ_s_head_offset(view)",
		"cpp":    "raw_.base + (situ_advance_u32(1,",
		"python": "self._write((advance(1,",
		"rust":   "situ_rt::write_be(self.bytes, situ_rt::advance(1,",
	}[target]

	assert composed in source


def test_a_tag_covering_a_dynamic_region_names_no_missing_function() -> None:
	"""An `authenticated` region is not a member -- it owns no bytes -- so it
	gets no offset function, and two callers named one anyway: the covered
	span a tag hands its algorithm, and `validate`'s bounds check. The
	generated C did not compile, for a tag over a region behind a
	variable-length member.

	Its start is its first member's, which is the rule `_region_end` already
	states for its end."""
	source = sources(COVERED_DYNAMIC)["c"]

	assert "situ_s_body_offset" not in source
	assert "uint32_t start = situ_s_head_offset(view);" in source


#: A sealed region whose interior the data sizes: a run of wide values, then a
#: scalar behind it.
SEALED_RUN = (
	"codec seal { length_preserving; seekable; authenticated;"
	" invertible; deterministic; }\n"
	"impl seal extern \"my_seal\";\n"
	"struct s {\n"
	"\tu8 n;\n"
	"\tsealed body(seal) { u16 vals[n]; u16 trailer; }\n"
	"\ttag u8 mac[16] covers(body);\n"
	"}\n"
)


@pytest.mark.parametrize("target", sorted(BACKENDS))
def test_a_sealed_run_is_reached_through_the_gate(target: str) -> None:
	"""Section 14.3's claim is that the interior cannot be had without a
	verification token, and the indexed accessor family was the one written
	without one: every element of a `u16 x[4]` inside a sealed region was
	readable in C through a plain view, while the scalar beside it demanded
	the gate. The other three emitted nothing at all for it."""
	source = sources(SEALED_RUN)[target]
	gated = {
		"c":      "situ_s_body_vals_get(situ_s_body_t gate, uint32_t index)",
		"cpp":    "std::uint16_t vals(std::uint32_t index) const noexcept",
		"python": "def vals(self, index: int) -> int:",
		"rust":   "pub fn vals(&self, index: usize) -> Result<u16> {",
	}[target]

	assert gated in source
	if target == "c":
		# The one that had it wrong says so exactly: no accessor for a sealed
		# member takes a bare view.
		assert "situ_s_body_vals_get(situ_view_t" not in source


def test_a_member_inside_a_region_is_placed_from_the_region() -> None:
	"""What precedes `body.trailer` is what precedes `body`, and then `body`'s
	own earlier members. The walk was over the struct's own members and never
	met a dotted one, so it never stopped: it summed the whole struct, the
	region itself and the *tag after it*, and C -- the only backend that
	emitted an accessor for such a member -- read it seventeen bytes past
	where it is."""
	schema   = parse_text(PREAMBLE + SEALED_RUN)
	resolved = resolve(schema, solve(schema))
	source   = "\n".join(generate_c(schema, resolved, "unit").files().values())

	assert "uint32_t offset = 1u;" in source
	assert "offset = 17u;" not in source
