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
	# Python has no constants; the bits appear as literals at the call sites.
	assert "mark_dirty(2)"  in source["python"]
	assert "clear_dirty(2)" in source["python"]


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


@pytest.mark.parametrize("target", ["python", "rust"])
def test_a_backend_without_delimiters_says_so(target: str) -> None:
	"""Rather than raising `AssertionError: offset is dynamic` out of the
	layout module, which is what reaching `offset_bytes` on a scanned member
	did. A traceback tells a user nothing about their schema and looks like a
	crash because it is one."""
	from situc.diagnostics import SituError

	schema   = parse_text(PREAMBLE + DELIMITED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	with pytest.raises(SituError, match="cannot emit a delimited member yet"):
		emit(schema, resolved, "unit")


@pytest.mark.parametrize("target,marker", [
	("c", "situ_s_line_span"),
	("cpp", "line_span()"),
])
def test_the_backends_that_do_have_them(target: str, marker: str) -> None:
	"""The other half of the claim above. A gap declared where there is none
	sends a reader designing around a limit that is not there (invariant 12),
	so the two tests are written together -- and this one is what makes the
	first fail the day a backend catches up and nobody edits the list."""
	schema   = parse_text(PREAMBLE + DELIMITED)
	resolved = resolve(schema, solve(schema))
	emit, _  = BACKENDS[target]

	assert marker in "\n".join(emit(schema, resolved, "unit").files().values())
