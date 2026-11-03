"""Capability-conformance checks over the Python accessors (sections 18, 22).

26.829 found that section 1's four artifacts reach different numbers of its
four languages, and `gen-checks` reached C alone: `situc/codegen/c/checks.py`
is 3239 lines over 70 functions, emitting nine kinds of check and asserting
the `MUTATE`, `OFFSET` and `SIZE` axes of the capability map.

**This is the first of those nine kinds, in Python, and the file it emits
says so.** Porting all nine is a port rather than an increment and does not
belong at the tail of a run; starting with one that stands alone does. The
kind chosen is the bounds check, for three reasons: it is one assertion per
struct rather than per field, it needs nothing but the acquiring call, and it
is the one section 12.3 calls the single check everything after it trusts --
so a backend that got it wrong would have every other guarantee resting on
nothing.

`unittest` rather than pytest, for the reason `python/vectors.py` gives:
section 24 requires `situc` to run from a bare interpreter with no
third-party packages, and a suite handed to a user should ask no more.
"""

from __future__ import annotations

from typing import Literal

from situc import __version__, ast
from situc.capability import Axis
from situc.codegen import PROBE_BYTES, refuse_parameters
from situc.codegen.python.emit import py_name
from situc.layout import BITS_PER_BYTE, Placement
from situc.propagate import Resolved
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import own_entries
from situc.types import ScalarKind

#: The nine kinds `--target c` emits, so the generated file can name the
#: eight it does not. Written out rather than derived, because the C emitter
#: names them in the test identifiers it builds and there is no list to
#: read -- which is itself worth knowing for whoever ports the rest.
C_KINDS = (
	"refuses a short buffer",
	"a field occupies its claimed bytes",
	"a field round-trips in place",
	"a field decodes a known encoding",
	"a nested member starts where the map says",
	"`must_eq` is enforced",
	"`reserved` must be zero",
	"an enum rejects a value no member names",
	"a framing reader reassembles across every boundary",
)


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str, prefix: str = "situ") -> str:
	"""A `unittest` module holding each struct to its own minimum extent."""
	refuse_parameters(schema)

	enums   = {decl.name for decl in schema.enums()}
	checked = [struct for struct in resolved.structs.values()
	           if _has_a_minimum(struct)]
	lines = _preamble(basename, len(checked))

	if not checked:
		# A class with no methods is an `IndentationError`, which is how
		# `example/protobuf` failed before this existed. Said rather than
		# left empty: a module that imports and defines nothing reads as a
		# suite that passed.
		lines.extend([
			"\tdef test_nothing_here_has_a_minimum(self) -> None:",
			'\t\t"""No struct in this schema has a view with a minimum'
			' extent."""',
			"\t\tself.skipTest(\"every struct is a register, or its"
			" minimum is zero\")",
			"",
		])

	for struct in checked:
		lines.extend(_short_buffer(basename, struct))
		for entry in own_entries(struct):
			lines.extend(_occupies(basename, struct, entry.placement))
			lines.extend(_round_trip(basename, enums, struct, entry))
			lines.extend(_decodes(basename, enums, struct, entry.placement))

	lines.extend([
		"",
		'if __name__ == "__main__":',
		"\tunittest.main()",
	])
	return "\n".join(lines) + "\n"


def _has_a_minimum(struct: ResolvedStruct) -> bool:
	"""Whether a buffer can be one byte short of this struct at all.

	Three exclusions, and two of them cost a failing run before they were
	here -- found by running the generated suites over the corpus, which is
	the whole argument for that gate.

	**A register has no view at all.** It is a bus transaction rather than
	bytes off a wire, so its accessors take a device handle and there is no
	`at` to refuse anything; `example/register` raised
	`AttributeError: type object 'dma_status' has no attribute 'at'`.
	`c/fuzz.py` excludes registers for the same reason and says so, and
	`gen-dissector` has since it was written.

	**A zero-byte minimum has no short buffer.** `at` on an empty message
	is the degenerate case rather than a refusal, so the check would assert
	a refusal that correctly does not happen. `example/protobuf` is a bare
	`tlv` region whose minimum is zero, and `c/fuzz.py` carries a warning
	about this exact filter: there it dropped protobuf out of fuzzing
	entirely and left a harness that exercised nothing. Here the struct
	genuinely has nothing to check, so excluding it is right -- and leaving
	the module with an empty class body is not, which is the other half of
	what protobuf found.
	"""
	return (struct.layout.is_byte_sized
	        and struct.layout.register is None
	        and struct.layout.size_bytes > 0)


#: How the count of kinds reads in prose, so the sentence naming it and the
#: list it counts cannot disagree. They did: the sentence said "Two" while
#: the list below it had grown to three, and nothing compared them -- a
#: value written in two places is the shape this tree keeps finding. It is
#: derived from `DONE` now, and `test_the_preamble_counts_its_own_kinds`
#: holds the two together.
_COUNT_WORD = ("no", "One", "Two", "Three", "Four", "Five",
               "Six", "Seven", "Eight", "Nine")

#: The kinds this backend emits: the first `DONE` of `C_KINDS`. One number,
#: so adding a kind moves the sentence, the emitted list and the declined
#: list together.
DONE = 4


def _preamble(basename: str, structs: int) -> list[str]:
	return [
		f'"""Generated by situc {__version__} from {basename}.situ'
		f' -- do not edit.',
		"",
		"Capability-conformance checks over the Python accessors: the",
		"accessors held to the map they were generated beside.",
		"",
		f"    python3 -m unittest {basename}_checks",
		"",
		f"**{_COUNT_WORD[DONE]} of the {_COUNT_WORD[len(C_KINDS)].lower()} kinds `situc gen-checks --target c` emits**,",
		f"over {structs} struct(s):",
		"",
		"  - a buffer one byte short of a struct's minimum must not yield a",
		"    view. Section 12.3 calls that the single check everything after",
		"    it trusts, which is why it was the one to port first -- a",
		"    backend that got it wrong would have every other guarantee",
		"    resting on nothing.",
		"  - a field occupies exactly the bytes the map claims. Each of them",
		"    must reach the value and nothing else may, which is checked by",
		"    flipping every byte of the buffer in turn.",
		"  - a field the map calls writable in place round-trips through its",
		"    setter and moves no byte outside its own claim. The map's own",
		"    verdict is the gate: where it does not say `InPlaceFixed`, no",
		"    check is emitted, so this asserts the `MUTATE` axis where the",
		"    two above assert `OFFSET` and `SIZE`.",
		"  - a byte-aligned field decodes a known encoding: a distinctive",
		"    pattern written into its claimed bytes must read back as the",
		"    number those bytes mean in the declared order. The expectation",
		"    is computed with `int.from_bytes`, which is the standard",
		"    library and not the backend's own read path -- so the check is",
		"    independent of the thing it checks.",
		"",
		f"The {_COUNT_WORD[len(C_KINDS) - DONE].lower()} `--target c` also emits and this does not:",
		"",
		*[f"  - {kind}" for kind in C_KINDS[DONE:]],
		"",
		"Saying which rather than leaving them out silently: a suite that",
		"checks one of nine things and does not say so reads as the whole",
		"map having been checked.",
		'"""',
		"",
		"from __future__ import annotations",
		"",
		"import unittest",
		"",
		"import situ_runtime",
		"",
		f"import {basename}",
		"",
		"",
		"class Checks(unittest.TestCase):",
	]


def _decodes(basename: str, enums: set[str], struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A byte-aligned field reads the number its bytes mean.

	The other three kinds ask WHERE a value comes from and whether writing
	it disturbs anything. This one asks WHAT the bytes mean -- the byte
	order, the width and the signedness -- which is the part of the map a
	reader of the generated code is most likely to take on trust.

	**The expectation is computed with `int.from_bytes`**, the standard
	library, rather than by the rule the backend reads with. That is what
	keeps the check independent of the thing it checks: an emitter that had
	the byte order backwards would produce a getter and an expectation that
	agreed, and this would still fail.

	`0x81` leads the pattern so the top bit is set, which is what makes a
	signed field's sign extension part of the claim rather than an untested
	corner.

	Byte-aligned only. A bit-packed field's value is a shift and a mask
	inside its byte, so the pattern and the expectation both need the bit
	position -- a different computation, and one the next pass should take
	rather than fold in here where it would read as the same check.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None or placement.type_name in enums:
		return []
	if placement.offset_bits % BITS_PER_BYTE or placement.size_bits % BITS_PER_BYTE:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	# `int.from_bytes` is the independent implementation, so it has to be an
	# implementation OF the thing being checked. It is that for a plain
	# integer and for nothing else, so the test is a whitelist rather than a
	# list of kinds to skip: a scalar kind added later declines until
	# somebody writes the independent decoder for it.
	#
	# `bcd2` is the measured case. The corpus's RTC holds its year as packed
	# decimal, where each nibble is a digit -- so the byte 0x81 means 81 and
	# the getter was right to say so against this check's 129. Fixed point is
	# the other exclusion and a subtler one: the two `*FIXED` fields pass
	# today, because the getter hands back the stored integer and the
	# schema's own comment says the caller applies the scale. But `REPR` says
	# only `ValueConverted` for both those and for BCD, where the getter DOES
	# convert -- so the map does not say whose the scaling is, and a check
	# that passed by relying on the answer would be resting on something
	# unstated. Recorded at 26.832 rather than depended on.
	if placement.scalar.kind not in (ScalarKind.UINT, ScalarKind.SINT):
		return []
	if placement.scalar.frac_bits:
		return []

	# **`--target c` declines a covered field here and this does not**, which
	# is a difference between the backends rather than a gap in one. C's
	# reason is that a covered field is "reached through the coverage
	# machinery, not a plain read"; in Python it is a plain read --
	# `icmp_message.type` is `repr=MemoryIdentical` over `self._read`, and
	# coverage costs it only its setter, which the round-trip kind above
	# already gates on. 40 of the corpus's byte-aligned scalars are covered,
	# and they are a real part of the 129 the sabotage above fails, so
	# copying C's exclusion would have thrown away checks that work.

	width = placement.size_bits // BITS_PER_BYTE
	if width < 1 or width > 8:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if first + width > size:
		return []

	# **Asked positively, because there are more than two answers.** The
	# first version of this read `"big" if endian is BIG else "little"`,
	# which is true of the two cases that motivated it and quietly wrong
	# about every other -- the defect shape situ keeps finding, a two-branch
	# predicate absorbing a third state into one arm.
	#
	# There are two such states in the corpus and `endian native` was only
	# the one I had thought of. It means the host decides, so no single
	# number is what the bytes mean. The other is `endian_marker`: TIFF's
	# first two bytes say which order the rest of the file uses, so the
	# resolved placement carries `endian=None` and the order is a runtime
	# value. That `None` fell into the `little` arm, and the check failed
	# against an accessor that was reading correctly.
	if placement.endian is ast.Endian.BIG:
		order: Literal["big", "little"] = "big"
	elif placement.endian is ast.Endian.LITTLE:
		order = "little"
	else:
		return []

	pattern  = bytes(PROBE_BYTES[:width])
	expected = int.from_bytes(pattern, order,
			signed=placement.scalar.signed)

	name    = py_name(f"{struct.name}_{placement.name}")
	acquire = (f"{basename}.{py_name(struct.name)}.at(msg, 0"
	           + ("" if struct.layout.is_fixed_size else f", {size}") + ")")
	return [
		f"	def test_{name}_decodes_a_known_encoding(self) -> None:",
		f'		"""{width} byte(s) at {first}, {order} order,'
		f' signed={placement.scalar.signed}."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {acquire}",
		"",
		f"		raw[{first}:{first + width}] = bytes.fromhex"
		f'("{pattern.hex()}")',
		"",
		f"		# {expected} is what those bytes mean in {order} order,",
		"		# computed by `int.from_bytes` -- the standard library,",
		"		# not the rule the accessor reads with. An emitter with",
		"		# the byte order backwards would agree with itself and",
		"		# still fail here.",
		f"		self.assertEqual(view.{py_name(placement.name)},"
		f' {expected},',
		f'			"{placement.name} does not decode its own bytes")',
		"",
	]


def _round_trip(basename: str, enums: set[str],
		struct: ResolvedStruct, entry: Resolved) -> list[str]:
	"""A field the map calls writable in place round-trips and moves nothing.

	**The map's own verdict is the gate**, which is the part of this check
	worth getting right. Where `mutate` is not `InPlaceFixed` the field is
	not claimed to be writable without moving anything, so no check is
	emitted -- and a backend that emitted a setter anyway would be caught by
	the absence of a check rather than by one, which is why the generated
	module says how many it emitted.

	This is the first of the kinds here to assert `MUTATE`; the two above
	assert `OFFSET` and `SIZE`.

	Writes `1` into a zeroed buffer, which is in range for any scalar of at
	least one bit and is a change from what is there, so a setter that did
	nothing would be caught -- which writing the value back unchanged would
	not. Enum-typed fields are declined for the same reason the vector
	suites decline them in Rust: `1` need not name a member, and the getter
	is entitled to refuse a value the enum does not have.
	"""
	placement = entry.placement
	if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
		return []

	# **The gate is two axes, not one**, and the second cost five schemas
	# before it was here. `mutate = InPlaceFixed` says writing the field
	# moves no bytes; `auth = Covered` says writing it leaves an
	# authentication tag stale, and the backend withholds the plain setter
	# for that reason (section 14.2). So a field can be writable in place
	# and have no setter at all -- `tcp_header.urgent` and
	# `usb.token.address` are both, and both raised
	# `AttributeError: property 'urgent' of 'tcp_header' object has no
	# setter`. Writing one is a different operation with a finalize after
	# it, which is its own check rather than this one.
	if entry.vector.get(Axis.AUTH).base != "Uncovered":
		return []
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if placement.array_count is not None:
		return []
	if placement.type_name in enums:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	last  = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if last >= size:
		return []

	name    = py_name(f"{struct.name}_{placement.name}")
	acquire = (f"{basename}.{py_name(struct.name)}.at(msg, 0"
	           + ("" if struct.layout.is_fixed_size else f", {size}") + ")")
	return [
		f"	def test_{name}_round_trips_in_place(self) -> None:",
		f'		"""The map calls `{struct.name}.{placement.name}` writable in'
		f' place."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {acquire}",
		"",
		f"		view.{py_name(placement.name)} = 1",
		"",
		"		# Read back through a FRESH view. `InPlaceFixed` says",
		"		# writing this field moves no bytes; it does not say a",
		"		# view taken before the write survives it.",
		"		# `test/schema/shared`'s `len` drives another member's",
		"		# extent, so writing it bumps the message generation and",
		"		# the old view is correctly refused with",
		"		# `StaleViewError` -- section 12.3's typestate working,",
		"		# not a field that failed to round-trip. Whether bytes",
		"		# MOVED is asked of `raw` below, which no generation",
		"		# affects.",
		f"		view = {acquire}",
		f'		self.assertEqual(view.{py_name(placement.name)}, 1,'
		f' "{placement.name} did not take 1")',
		"",
		f"		for i in range({size}):",
		f"			if {first} <= i <= {last}:",
		"				continue",
		"			self.assertEqual(raw[i], 0,",
		f'				f"writing {placement.name} moved byte {{i}}")',
		"",
	]


def _occupies(basename: str, struct: ResolvedStruct,
		placement: Placement) -> list[str]:
	"""A field reads exactly the bytes the map claims for it, and no others.

	The heart of the artifact: the map is a claim about which bytes a value
	comes from, and this is the only check that reads it back. Flipping each
	byte in turn and asking whether the getter noticed tests the claim from
	outside, which is what makes it independent of how the accessor was
	emitted.

	`noticed` rather than "every claimed byte changes the value": a field
	narrower than its byte, or one whose value does not depend on every bit
	of it, legitimately does not notice a flip in a bit it does not use. The
	C version makes the same distinction and for the same reason -- the
	claim is that the value comes from THESE bytes and from no others, not
	that it uses all of them.

	Declined where the claim cannot be read back:

	- a dynamic offset, where the map has no byte to name;
	- a field with no getter -- `reserved`, a marker, a `parameter`;
	- a text number, whose getter RAISES on digits that are not digits, so
	  flipping a byte does not change the value but refuses to produce one,
	  and the check would read a refusal as a change;
	- **a field whose extent is not fixed**, which has no definite claimed
	  range to flip. `udp_header.payload` is `[remaining]`, so at the
	  struct's minimum it is ZERO bytes long and flipping its range changes
	  nothing -- the check asserted that some claimed byte reaches the value
	  and 22 of the corpus's 42 schemas said otherwise, every one of them
	  correctly. The map claims a byte range for a field at a static offset
	  with a fixed extent, and for no other.
	"""
	if placement.offset_bits is None or placement.kind != "field":
		return []
	if placement.scalar is None or placement.radix is not None:
		return []
	if placement.size_max_bits != placement.size_bits:
		return []
	if not struct.layout.is_byte_sized or struct.layout.size_bytes == 0:
		return []

	first = placement.offset_bits // BITS_PER_BYTE
	last  = (placement.offset_bits + placement.size_bits - 1) // BITS_PER_BYTE
	size  = struct.layout.size_bytes
	if last >= size:
		return []

	# A byte run's property is a `memoryview`, which compares equal to
	# bytes; `bytes(...)` is what makes a difference printable and is the
	# same cast the vector suites use.
	read = f"view.{py_name(placement.name)}"
	if placement.array_count is not None:
		read = f"bytes({read})"

	name = py_name(f"{struct.name}_{placement.name}")
	return [
		f"	def test_{name}_occupies_its_claimed_bytes(self) -> None:",
		f'		"""The map claims bytes {first}..{last} of'
		f' `{struct.name}` for `{placement.name}`."""',
		f"		raw = bytearray({size})",
		"		msg = situ_runtime.Message(raw)",
		f"		view = {basename}.{py_name(struct.name)}.at(msg, 0"
		+ ("" if struct.layout.is_fixed_size else f", {size}") + ")",
		"",
		f"		before = {read}",
		"		noticed = False",
		f"		for i in range({first}, {last} + 1):",
		"			raw[i] ^= 0xFF",
		f"			if {read} != before:",
		"				noticed = True",
		"			raw[i] ^= 0xFF",
		f'		self.assertTrue(noticed, "no claimed byte reaches'
		f' {placement.name}")',
		"",
		f"		for i in range({size}):",
		f"			if {first} <= i <= {last}:",
		"				continue",
		"			raw[i] ^= 0xFF",
		f"			self.assertEqual({read}, before,"
		f' f"byte {{i}} must not reach {placement.name}")',
		"			raw[i] ^= 0xFF",
		"",
	]


def _short_buffer(basename: str, struct: ResolvedStruct) -> list[str]:
	minimum = struct.layout.size_bytes
	short   = minimum - 1
	# A variable struct's `at` takes the message length and a fixed one's
	# does not, which is the Python backend's own branch on
	# `layout.is_fixed_size` -- the thing 26.829's vector emitter got wrong
	# for 13 of 27 schemas before it was run.
	extent = "" if struct.layout.is_fixed_size else f", {short}"

	return [
		f"\tdef test_{py_name(struct.name)}_refuses_a_short_buffer"
		f"(self) -> None:",
		f'\t\t"""`{struct.name}` needs {minimum} bytes; {short} must refuse."""',
		f"\t\traw = bytearray({short})",
		"\t\tmsg = situ_runtime.Message(raw)",
		"",
		"\t\twith self.assertRaises(situ_runtime.BoundsError):",
		f"\t\t\t{basename}.{py_name(struct.name)}.at(msg, 0{extent})",
		"",
	]
