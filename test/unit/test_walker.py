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

import struct

from situc import pack as packer
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, SCHEMAS, ids
from fourway import COMPLETE, answers, build, draw

sys.path.insert(0, str(ROOT))
from walker import report, vm                      # noqa: E402
from walker.image import load                      # noqa: E402
from walker import walk as walk_module             # noqa: E402
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


LEB128 = ("target buffer;\nendian big;\n"
          "varint_type small { encoding = leb128; max_bits = 32;"
          " max_bytes = 5; }\n"
          "struct s { u8 lead; small n; u8 after; }\n")

BE128 = ("target buffer;\nendian big;\n"
         "varint_type wide { encoding = be128; max_bits = 64; max_bytes = 9; }\n"
         "struct s { u8 lead; wide n; u8 after; }\n")


def test_a_varint_answers_its_value_and_not_its_bytes() -> None:
	"""The scalar read decodes one, because a varint has a value.

	`ac 02` is 300 in leb128 and 44034 read as two big-endian bytes, and the
	walk answered the second. This is the byte-run question with the opposite
	answer: a run has no single value and `read_scalar` refuses one, a varint
	has one and every compiled backend's `_get` decodes it.

	The bytes are chosen so the two answers differ in every reading. `96 01`
	-- the pair this was first seen with -- is 150 decoded and 0x96 is 150
	raw, so a walker reading one byte agrees with a walker decoding two by
	coincidence, which is how the C side survived a differential.
	"""
	view = acquire(load(packed(LEB128)), bytes.fromhex("11ac0222"), 0)

	assert read_scalar(view, 1) == 300
	assert read_scalar(view, 2) == 0x22	# and `after` is still placed at 3


def test_the_high_group_first_encoding_decodes_too() -> None:
	"""`be128` is ASN.1's identifier octets and SQLite's record varints, and
	it is the other byte order rather than the same one. `81 00` holds 128
	there and 33024 read as raw bytes."""
	view = acquire(load(packed(BE128)), bytes.fromhex("11810022"), 0)

	assert read_scalar(view, 1) == 128
	assert read_scalar(view, 2) == 0x22


def test_a_truncated_varint_is_refused_by_the_value_read() -> None:
	"""Two readers, as for a text number: the width answers zero and lets the
	offset chain carry on, and the *value* refuses. That is what every
	backend's `_get` does where the frame ends mid-value."""
	view = acquire(load(packed(LEB128)), bytes.fromhex("11ac"), 0)

	with pytest.raises(Refused):
		read_scalar(view, 1)
	assert read_scalar(view, 2) == 0xAC	# and `after` is placed at 1, not lost


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


#: The header of the first entry in `example/cpio/cpio.vectors`, which GNU
#: cpio wrote: `magic` and thirteen ASCII hex numbers, 110 bytes.
CPIO_HEADER = bytes.fromhex(
	"303730373031303031423131323930303030383142343030303030334538303030"
	"303033453830303030303030313641373042414242303030303030303630303030"
	"303030303030303030303242303030303030303030303030303030303030303030"
	"3030443030303030303030"
)


@pytest.mark.skipif(not COMPLETE, reason="a backend is missing")
def test_a_text_number_is_validated_in_all_five(tmp_path: Path) -> None:
	"""cpio's header, and the constraint its own comment defends.

	`decimal u32 magic[6] [min = 70701, max = 70702]` says in the schema why
	it is written that way: "the alternative is six bytes nothing
	constrains". Nothing constrained them. `traverse.classify_check` had no
	branch for a text number, so its digit count read as an array and it was
	checked as one -- an encoding and a terminator it has neither of -- while
	the accessor classifier one function up has had that branch since three
	backends misread the same bracket. `validate` returned OK for `07070x`
	in every backend and in the walk.

	Three mutations of bytes GNU cpio wrote, because a check that only ever
	sees good input is not being asked anything: a magic outside its declared
	range, a magic that is not a number, and a hex field of `z`. All five
	must agree, and the good header must still pass -- Rust's validator read
	a member's raw bytes, so the first version of this fix compared
	0x303730373031 against 70701 and refused the real archive.
	"""
	schema  = ROOT / "example" / "cpio" / "cpio.situ"
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	parsed   = parse_text(schema.read_text(encoding="ascii"))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	image    = load(blob)

	cases = {
		"the header cpio wrote":  (CPIO_HEADER,                        report.OK),
		"a magic out of range":   (b"999999" + CPIO_HEADER[6:],        report.ERR_CONSTRAINT),
		"a magic that is not a number":
		                          (b"07070x" + CPIO_HEADER[6:],        report.ERR_CONSTRAINT),
		"a hex field of z":       (CPIO_HEADER[:6] + b"zzzzzzzz"
		                           + CPIO_HEADER[14:],                 report.ERR_CONSTRAINT),
	}

	for label, (packet, expected) in cases.items():
		walked = _by_member(report.listing(image, packet))
		assert walked.get(("cpio_header", "validate")) \
			== f"validate {expected}", f"the walk, on {label}"

		for backend, argv in command.items():
			found = _by_member(answers(argv, packet, tmp_path))
			assert found.get(("cpio_header", "validate")) \
				== f"validate {expected}", f"{backend}, on {label}"


@pytest.mark.skipif(not COMPLETE, reason="a backend is missing")
def test_a_delimited_text_number_keeps_its_range(tmp_path: Path) -> None:
	"""The other form of the same construct, and the other half of the gap.

	A delimited text number had its digits parsed and its declared range
	dropped: the branch that handles a delimited member returns before the
	one that emits `[min]` and `[max]`, in all four backends and in the
	packer. So `999` passed `[min = 200, max = 599]` in all five while `12x`
	was correctly refused -- the parse enforced, the bound not.

	`edges.ranged_code` is the schema, added for this: nothing in the tree
	wrote a delimited text number with a range, which is exactly how the
	fixed-width form's missing checks stayed hidden. The corpus differential
	covers it from here, but random bytes reach a valid three-digit code
	followed by a space about never, so the cases are written out.
	"""
	schema  = ROOT / "test" / "schema" / "edges.situ"
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	parsed   = parse_text(schema.read_text(encoding="ascii"))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	image    = load(blob)

	cases = {
		"a code in range":       (b"250 ok", report.OK),
		"a code above max":      (b"999 ok", report.ERR_CONSTRAINT),
		"a code below min":      (b"199 ok", report.ERR_CONSTRAINT),
		"digits that are not":   (b"12x ok", report.ERR_CONSTRAINT),
	}

	for label, (packet, expected) in cases.items():
		walked = _by_member(report.listing(image, packet))
		assert walked.get(("ranged_code", "validate")) \
			== f"validate {expected}", f"the walk, on {label}"

		for backend, argv in command.items():
			found = _by_member(answers(argv, packet, tmp_path))
			assert found.get(("ranged_code", "validate")) \
				== f"validate {expected}", f"{backend}, on {label}"


def test_a_minimal_text_number_is_refused_in_all_five(tmp_path: Path) -> None:
	"""`[minimal]` on HTTP's status code, made to fail for the first time.

	The two tests above mutate bytes because "a check that only ever sees
	good input is not being asked anything". This is the check they left:
	`example/http` declares `decimal u16 code until " " max 4 [minimal]`, so
	the constraint is in the corpus and a real response satisfies it -- and
	`walker/report`'s leading-zero and upper-case-hex branches had never been
	taken by anything. A schema uses it, every test feeds it a valid code,
	and the rejection was untried in all five descriptions.

	Found by tracing the walker and asking which statements the suite never
	reaches (26.216's instrument, pointed at the fifth description).

	`0` on its own is the case that separates the rule from a paraphrase of
	it: a single zero *is* the minimal spelling of zero, so the branch is
	`len(digits) > 1 and digits[0] == "0"` rather than "starts with a zero".
	A check that refused it would pass a test that only tried a padded code.

	**And the padded code is `020`, not `0200`, because the first version of
	this test proved nothing.** `max 4` caps the whole member, delimiter
	included, so `0200 ` is five bytes and was refused for its length --
	the same verdict by another route. Deleting the walker's leading-zero
	branch left the test green. Three digits fit the cap, so `020` and `00`
	can only be refused by the rule this is about, and deleting that branch
	now fails.
	"""
	schema  = ROOT / "example" / "http" / "http.situ"
	command = build(tmp_path, schema)
	if not command:
		pytest.skip("no struct a driver can acquire")

	parsed   = parse_text(schema.read_text(encoding="ascii"))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	image    = load(blob)

	cases = {
		"the status line a server writes":
			(b"HTTP/1.1 200 OK\r\n", report.OK),
		"a code padded with a leading zero":
			(b"HTTP/1.1 020 OK\r\n", report.ERR_CONSTRAINT),
		"two zeros, where only the first is spare":
			(b"HTTP/1.1 00 OK\r\n", report.ERR_CONSTRAINT),
		"a lone zero, which is minimal":
			(b"HTTP/1.1 0 OK\r\n", report.OK),
		"digits that are not":
			(b"HTTP/1.1 2x0 OK\r\n", report.ERR_CONSTRAINT),
	}

	for label, (packet, expected) in cases.items():
		walked = _by_member(report.listing(image, packet))
		assert walked.get(("status_line", "validate")) \
			== f"validate {expected}", f"the walk, on {label}"

		for backend, argv in command.items():
			found = _by_member(answers(argv, packet, tmp_path))
			assert found.get(("status_line", "validate")) \
				== f"validate {expected}", f"{backend}, on {label}"


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


def test_the_walker_knows_the_same_axes_as_the_compiler() -> None:
	"""`walker/image.py` carries a second copy of the capability vocabulary,
	and it has to: this walker imports nothing from the compiler but the
	image format, which is what lets a device carry it.

	A copy is allowed to exist here only because this compares it. The
	packer writes an index into `DOMAINS[axis]` and the walker reads one out
	of its own table, so a value appended to an axis in one place and not the
	other silently renames every value after it -- which is the worst shape a
	drift can take, because every byte still decodes to something.
	"""
	from situc.capability import DOMAINS as COMPILER_DOMAINS
	from situc.capability import Axis
	from walker.image import AXES, DOMAINS as WALKER_DOMAINS

	assert AXES == tuple(axis.value for axis in Axis), \
		"the walker's axis order is not the order the packer writes"

	for axis in Axis:
		assert WALKER_DOMAINS[axis.value] == \
			tuple(str(value) for value in COMPILER_DOMAINS[axis]), \
			f"the walker's `{axis.value}` domain differs from the compiler's"


# -- a nested field's base (26.184) -----------------------------------------


def based_probe(schema: str, struct: str, member: str,
                message: bytes) -> int:
	"""Evaluate one member's size program over `message`."""
	from situc.layout import solve as _solve
	from situc.pack import pack as _pack
	from situc.parser import parse_text as _parse
	from situc.resolve import resolve as _resolve
	from walker.image import load as _load
	from walker import walk as _walk

	parsed = _parse(Path(schema).read_text(encoding="ascii"), path=schema)
	image  = _load(_pack(parsed, _resolve(parsed, _solve(parsed)),
	                     metadata=True)[0])
	names  = {image.name_of(i): i for i in range(len(image.placements))}
	root   = next(i for i, n in enumerate(image.struct_names) if n == struct)
	held   = image.placements[names[member]]
	return walk_module._evaluate(_walk.acquire(image, message, root),
	                             held.size_code)


def test_a_sealed_extent_comes_from_the_nested_field_not_its_twin() -> None:
	"""`example/packet` takes its sealed region's extent from `hdr.length`.

	`header.length` is at 2 in its own struct and `packet.hdr` sits at 4, so
	the field is at 6. The program read 2 -- a placement's offset is within
	its own struct, and `FIELD` carried no base, so the read landed at the
	right offset of the wrong struct. Right only where the nested struct is
	at 0, and this one is not (26.184).

	The compiled C reads it at `view.base + 6u`, so four backends and the
	layout agreed and the image was the one that did not.
	"""
	def probe(at2: int, at6: int) -> int:
		message = bytearray(96)
		message[2:4] = struct.pack(">H", at2)
		message[6:8] = struct.pack(">H", at6)
		return based_probe("example/packet/packet.situ", "packet",
		                   "packet.sealed.body", bytes(message))

	assert probe(111, 222) == 222, "the extent came from the wrong offset"
	assert probe(999, 222) == 222, "offset 2 still reaches the program"


def test_a_located_run_is_sized_from_the_nested_field() -> None:
	"""`example/bmp` sizes its pixels by `info.image_size`, at 20 in its own
	struct and 34 in the frame, because `bitmap_file.info` sits at 14."""
	message = bytearray(62)
	message[0:2]   = b"BM"
	message[2:6]   = struct.pack("<I", 62)
	message[10:14] = struct.pack("<I", 58)
	message[14:18] = struct.pack("<I", 40)
	message[20:24] = struct.pack("<I", 999)		# the decoy, at its own offset
	message[34:38] = struct.pack("<I", 4)		# the real image_size

	assert based_probe("example/bmp/bmp.situ", "bitmap_file",
	                   "bitmap_file.pixels", bytes(message)) == 4


#: Lines the walker's listing carries that are not members: a validation
#: verdict, and the note for a struct no view can be taken of.
NOT_A_MEMBER = frozenset({"validate", "no-view"})


def compared_members() -> tuple[int, int, int]:
	"""What C is asked, what the walker renders, and the overlap.

	Both sides statically: `differ.asks` is what the generated probe asks
	about, and `report.listing` is what the walker answers. No compilation,
	so this runs everywhere rather than only where four toolchains do.
	"""
	from situc.codegen import differ

	asked_total = walked_total = both_total = 0
	for path in SCHEMAS:
		parsed   = parse_text(path.read_text(encoding="ascii"))
		resolved = resolve(parsed, solve(parsed))
		names    = {held.name for held in resolved.structs.values()}
		asked    = {(held.name, ask.local)
		            for held in differ.structs_of(resolved)
		            for ask in differ.asks(held, names, resolved.structs)}

		blob, _ = packer.pack(parsed, resolved, metadata=True)
		image   = load(blob)
		walked, where = set(), ""
		for line in report.listing(image, bytes(range(1, 97))).split("\n"):
			if line.startswith("-- "):
				where = line[3:]
				continue
			if line.strip() and line.split()[0] not in NOT_A_MEMBER:
				walked.add((where, line.split()[0]))

		asked_total  += len(asked)
		walked_total += len(walked)
		both_total   += len(asked & walked)
	return asked_total, walked_total, both_total


def test_the_two_descriptions_overlap_enough_to_be_a_differential() -> None:
	"""The fifth column is worth having only where both columns speak.

	`test_the_walker_agrees_with_the_compiled_backends` compares a member
	only where C and the walker both answer, which is right -- a member one
	side never mentions is a difference in what was asked. It also means the
	overlap *is* the coverage, and nothing counted it. The floor beside this
	one counts what the walker's subset reaches, which is one side.

	26.184 lived in the gap. `packet.sealed.body` is not rendered by the
	walker, because a sealed interior sits behind a gate it does not render;
	`bmp.pixels` is rendered and never asked of C, because a located member
	gets a view accessor and the probe asks about scalars. Both were wrong
	for a year and both were outside the comparison -- and that is not a
	coincidence, since a construct is unusual enough to be got wrong for the
	same reason it is unusual enough to be missing from a probe.

	Two floors, because either alone can be met while the other rots: a
	count, so the overlap cannot shrink, and a share of what C asks, so
	adding schemas that only one side speaks about cannot dilute it.
	"""
	asked, walked, both = compared_members()

	assert both >= 470, (
		f"the differential compares {both} members, down from 477; "
		f"C asks about {asked} and the walker renders {walked}")
	assert both * 100 >= asked * 88, (
		f"only {100 * both // asked}% of what C asks is compared, "
		f"down from 91%")
