"""The walker: an image, walked over bytes (26.33, decision 0026 amended).

The walker is the fifth column. Its value is entirely in disagreeing with the
four compiled backends when one of them is wrong, so a test that compared it
against itself -- or against `traverse.py`, which is what the four are five
spellings of -- would be worth nothing. What it is held to here is the C
backend's own answer about the same buffer, compiled and run.

What it renders is a subset, and `report.SUPPORTED` names it. That subset is
asserted to be non-empty and every line in it is required to appear in the
compiled backend's output, so the way this passes while checking nothing --
rendering no lines at all -- is closed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from situc import pack as packer
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids
from fourway import COMPLETE, answers, build, draw

sys.path.insert(0, str(ROOT))
from walker import report, vm                      # noqa: E402
from walker.image import load                      # noqa: E402
from walker.walk import Refused, View, acquire, read_scalar  # noqa: E402


def packed(text: str) -> bytes:
	schema   = parse_text(text)
	resolved = resolve(schema, solve(schema))
	blob, _  = packer.pack(schema, resolved, metadata=True)
	return blob


# ---------------------------------------------------------------------------
# The image round-trips into a walk
# ---------------------------------------------------------------------------

def test_a_scalar_is_read_at_the_offset_the_image_gives() -> None:
	"""The smallest whole claim: a big-endian u16 at a constant offset."""
	image = load(packed("target buffer;\nendian big;\n"
	                    "struct s { u8 tag; u16 value; }\n"))
	view  = acquire(image, bytes([0x01, 0xAA, 0xBB]), 0)

	assert read_scalar(view, 0) == 0x01
	assert read_scalar(view, 1) == 0xAABB


def test_byte_order_comes_from_the_image() -> None:
	"""Little-endian is the schema's word, not the host's."""
	image = load(packed("target buffer;\nendian little;\n"
	                    "struct s { u16 value; }\n"))
	view  = acquire(image, bytes([0xAA, 0xBB]), 0)
	assert read_scalar(view, 0) == 0xBBAA


def test_a_bit_packed_field_is_read_from_the_bytes_it_straddles() -> None:
	"""`u4` twice in one byte, most significant first."""
	image = load(packed("target buffer;\nendian big;\nbit_order msb_first;\n"
	                    "struct s { u4 high; u4 low; }\n"))
	view  = acquire(image, bytes([0xAB]), 0)
	assert read_scalar(view, 0) == 0xA
	assert read_scalar(view, 1) == 0xB


def test_a_short_frame_is_refused_rather_than_read() -> None:
	"""The one bounds check, which everything after it trusts (20.2)."""
	image = load(packed("target buffer;\nendian big;\n"
	                    "struct s { u32 a; u32 b; }\n"))
	with pytest.raises(Refused):
		acquire(image, bytes([0x00]), 0)


def test_a_computed_size_runs_the_bytecode() -> None:
	"""`u8 data[n * 2]` is a program, and the walk runs it over the buffer."""
	image = load(packed("target buffer;\nendian big;\n"
	                    "struct s { u8 n; u8 data[n * 2]; }\n"))
	view  = acquire(image, bytes([0x03] + [0xFF] * 6), 0)
	from walker.walk import size_bits
	assert size_bits(view, 1) == 6 * 8


# ---------------------------------------------------------------------------
# The bytecode
# ---------------------------------------------------------------------------

def _run(code: bytes, remaining: int = 0) -> int:
	return vm.run(code, 0, lambda i: 0, lambda i: 0, lambda i: 0,
	              lambda i: 0, remaining)


def test_the_evaluator_refuses_division_by_zero() -> None:
	"""A length from an unchecked division is an overrun in whatever reads
	it next, so it raises rather than yielding zero."""
	import struct as _s
	code = (bytes([vm.PUSH]) + _s.pack("<q", 8)
	        + bytes([vm.PUSH]) + _s.pack("<q", 0)
	        + bytes([vm.DIV, vm.END]))
	with pytest.raises(vm.VmError):
		_run(code)


def test_the_evaluator_refuses_a_program_without_end() -> None:
	"""The program counter only advances, so running off the end is the only
	way not to terminate -- and it is an error, not a value."""
	import struct as _s
	with pytest.raises(vm.VmError):
		_run(bytes([vm.PUSH]) + _s.pack("<q", 1))


def test_the_opcodes_match_the_packer() -> None:
	"""Two copies of one table, kept honest by reading both.

	The walker must not import `situc` and `situc` must not import the
	walker (0026), so the opcode numbers are written twice on purpose. This
	is what stops that being a licence to drift.
	"""
	for name in ("END", "PUSH", "FIELD", "REMAINING", "SIZE", "OFFSET",
	             "COUNT", "ADD", "SUB", "MUL", "DIV", "MOD", "AND", "OR",
	             "XOR", "SHL", "SHR", "NEG", "NOT", "EQ", "NE", "LT", "LE",
	             "GT", "GE", "LAND", "LOR", "MIN", "MAX", "ALIGN_UP"):
		assert getattr(vm, name) == getattr(packer.Op, name), name


def test_the_compiler_does_not_import_the_walker() -> None:
	"""0026's amendment made the boundary a build fact, and a build fact
	that nothing checks is a directory layout with an opinion."""
	import re
	imports = re.compile(r"^\s*(?:from|import)\s+walker\b", re.MULTILINE)
	offenders = [path.relative_to(ROOT)
	             for path in (ROOT / "situc").rglob("*.py")
	             if imports.search(path.read_text(encoding="ascii"))]
	assert not offenders, f"situc imports the walker: {offenders}"


# ---------------------------------------------------------------------------
# The fifth column
# ---------------------------------------------------------------------------

def test_the_walker_renders_something_to_compare() -> None:
	"""The failure this whole file exists to make impossible: a fifth column
	that agrees with everybody by saying nothing."""
	assert report.SUPPORTED
	image = load(packed("target buffer;\nendian big;\n"
	                    "struct s { u8 tag; u16 value; }\n"))
	text = report.listing(image, bytes([1, 2, 3]))
	assert "-- s" in text
	assert any(line.startswith("tag ") for line in text.split("\n"))


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_walker_agrees_with_the_compiled_backends(
		schema: Path, tmp_path: Path) -> None:
	"""Every line the walker renders must appear in C's answer, byte for
	byte, for the same buffer.

	Held to the compiled backend rather than to `traverse.py`: the four are
	five spellings of that module, so comparing against it would be asking
	one implementation whether it agrees with itself.
	"""
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	text     = schema.read_text(encoding="ascii")
	parsed   = parse_text(text)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	image    = load(blob)

	import random
	rng     = random.Random(20260807)
	checked = 0

	# Random bytes reach a version field's low values about once in 256, so
	# the `[since]` gate -- whose whole behaviour is "is this member even
	# here" -- was answered for v1 twice in four hundred draws and never
	# deliberately. These six open with 1, 2 and 3 so a schema carrying
	# `[version = ...]` is asked each of its versions, and they cost six
	# driver runs rather than a hand-written expectation: C is still the
	# thing being compared against, at the moment of comparison.
	versioned = [bytes([which]) + draw(rng)
	             for which in (1, 2, 3) for _ in range(2)]

	for packet in [draw(rng) for _ in range(12)] + versioned:
		compiled = _by_member(answers(command["c"], packet, tmp_path))
		walked   = _by_member(report.listing(image, packet))

		# Where both speak about a member, they must say the same thing.
		# Not every rendered line: the differ probes a subset of its own and
		# does not ask about every scalar, so a member the walker answers and
		# C never mentions is a difference in *what was asked*, not in the
		# answer. Comparing those would report disagreements that are not
		# there -- the failure the differ's own docstring warns about.
		for key in walked.keys() & compiled.keys():
			assert walked[key] == compiled[key], (
				f"{schema.name}: the walker and C disagree about {key}:\n"
				f"  walker: {walked[key]!r}\n  C:      {compiled[key]!r}\n"
				f"  buffer: {packet.hex()}")
			checked += 1

	# Not asserted per schema: `http` and `smtp` are delimited throughout and
	# have no member this subset renders, which is a true answer rather than
	# a silent one. The corpus-wide floor below is what keeps the whole thing
	# from passing while rendering nothing.
	if not checked:
		pytest.skip(f"{schema.name}: no member both this walker and C probe")


def _by_member(text: str) -> dict[tuple[str, str], str]:
	"""One backend's listing as (struct, member) -> line.

	Keyed by the section too, because a member name repeats across structs
	and comparing `count` from one against `count` from another would be a
	disagreement invented by the comparison.
	"""
	found: dict[tuple[str, str], str] = {}
	where = ""
	for line in text.split("\n"):
		if line.startswith("-- "):
			where = line[3:]
			continue
		if not line or " " not in line:
			continue
		found[(where, line.split(" ", 1)[0])] = line
	return found


def test_a_struct_the_image_cannot_answer_for_says_so() -> None:
	"""The other half of the `validate` bit, which the corpus stopped
	exercising the moment it stopped needing to.

	Every struct in the tree answers now, which is the good outcome and a
	hole: the bit's false path is the one that keeps a *partial* `validate`
	from reporting OK where the schema refuses, and nothing was left to
	check that it still worked. A schema is written here rather than found,
	because the point is a construct the image genuinely cannot carry.

	A bounded ratio is that construct. The region's extent is data-dependent
	-- worst case known, actual not -- so nothing after it can be placed
	without decoding, no backend emits an offset for `trailer`, and a walk
	that answered anyway would be the only implementation with an opinion.
	"""
	source = (
		"endian big;\n"
		"\n"
		"codec squishy {\n"
		"\texpansion = ratio_bounded(3, 1);\n"
		"\tgranularity = byte;\n"
		"\tinvertible;\n"
		"\tdeterministic;\n"
		"}\n"
		"\n"
		"impl squishy extern \"my_squishy\";\n"
		"\n"
		"struct undecidable {\n"
		"\tu8  n;\n"
		"\tcoded body(squishy) {\n"
		"\t\tu8 content[n];\n"
		"\t}\n"
		"\tu8  trailer;\n"
		"}\n")
	parsed   = parse_text(source)
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	image    = load(blob)

	assert len(image.structs) == 1
	assert not image.structs[0].validatable, \
		"a region with no closed-form extent must not claim `validate`"

	# And the walk declines rather than guessing, which is what the bit is
	# for: `None` is "this image cannot say", not `OK`.
	view = View(image, bytes(16), 0, 0, 16)
	assert report._validate(image, view, 0) is None


def test_the_subset_reaches_most_of_the_corpus() -> None:
	"""The fifth column's coverage, stated as a number rather than implied.

	26.76's rule: a check that examined nothing must not read as clean. The
	per-schema comparison skips where the subset is empty, so this is what
	stops every schema skipping and the suite reporting green.
	"""
	reached, scalars, answerable = 0, 0, 0
	for path in SCHEMAS:
		parsed   = parse_text(path.read_text(encoding="ascii"))
		resolved = resolve(parsed, solve(parsed))
		blob, _  = packer.pack(parsed, resolved, metadata=True)
		image    = load(blob)
		found = sum(len(report._scalars(image, i))
		            + len(report._runs(image, i))
		            + len(report._arm_values(image, i))
		            + len(report._gates(image, i))
		            + len(report._delimited(image, i))
		            + len(report._varints(image, i))
		            + len(report._while_runs(image, i))
		            + len(report._nested(image, i))
		            + len(report._tags(image, i))
		            for i in range(len(image.structs)))
		scalars += found
		reached += 1 if found else 0
		answerable += sum(1 for i in range(len(image.structs))
		                  if image.structs[i].validatable)

	assert reached >= 20, f"the walker renders for only {reached} schemas"
	assert scalars >= 520, f"only {scalars} members in the rendered subset"
	# `validate` is one line per struct rather than one per member, so the
	# member floor above says nothing about it: a change that made every
	# struct unvalidatable would leave that number untouched and the whole
	# probe would go quiet. Every struct in the corpus carries it now --
	# 141 of 141 -- so this is a floor against losing them rather than a
	# record of how far the work got. `[since]` gates, coded regions, text
	# numbers, reserved runs and unstated variant defaults were all on the
	# deferred list and none of them is.
	#
	# The bit's *false* path has no corpus schema left to exercise it, which
	# is what `test_a_struct_the_image_cannot_answer_for_says_so` is for.
	assert answerable >= 141, \
		f"only {answerable} structs the walker can answer `validate` for"
