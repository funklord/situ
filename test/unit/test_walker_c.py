"""The embedded walker, held to the Python one (0035).

The C walker is the walker decision 0026 was argued from -- a device whose
framing changes without a firmware rebuild -- and the Python one is the
fifth column of the differential check. So the check that matters is that
they agree: two independent readers of one image over the same bytes, which
is the same argument the four backends are held to.

What this build of the C walker does not render is refused by name, and the
tests assert the refusals as well as the answers. A walker that returned a
number for a member it could not place would be returning a wrong length
that reads exactly like a right one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse
from situc.resolve import resolve
from situc.diagnostics import Source
from walker.image import load
from walker import report, walk
from walker.image import NONE, Image
from walker.walk import (Refused, Unplaceable, acquire, offset_bits,
                         read_bytes, read_scalar, size_bits)

COMPILER = shutil.which("cc") or shutil.which("gcc")
WALKER   = ROOT / "walker" / "c"

#: The same flags `make test-c` builds generated code with. An embedded
#: walker that needs a relaxed warning set is one nobody can put in a build.
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion")

DRIVER = """#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "situ_walk.h"

SHOW
int main(int argc, char **argv)
{
	static uint8_t img[65536];
	static uint8_t msg[512];

	(void)show;	/* the width ask has no value to print */

	if (argc < 3) {
		return 2;
	}

	FILE *f = fopen(argv[1], "rb");
	if (!f) {
		return 2;
	}
	const size_t n = fread(img, 1, sizeof img, f);
	fclose(f);

	/* Poisoned before opening, in every case this file runs. A caller
	 * declares this struct on the stack, and `situ_walk_open` is what has to
	 * leave every table it did not find reading as empty -- it did not, and
	 * an image with no varint section searched whatever was there. A zero
	 * stack is not a test of that. */
	situ_walk_image image;
	memset(&image, 0xAA, sizeof image);
	if (situ_walk_open(&image, img, (uint32_t)n) != SITU_WALK_OK) {
		printf("malformed\\n");
		return 1;
	}

	uint32_t len = 0;
	for (const char *p = argv[2]; p[0] && p[1]; p += 2) {
		char pair[3];
		pair[0] = p[0];
		pair[1] = p[1];
		pair[2] = 0;
		msg[len++] = (uint8_t)strtoul(pair, NULL, 16);
	}

	/* Which struct, because the first one in an image is whichever the
	 * packer put there and a run's *container* is the interesting one. */
	const uint32_t shape = (argc > 3) ? (uint32_t)strtoul(argv[3], NULL, 10)
	                                  : 0u;

	uint32_t first = 0;
	uint32_t count = 0;
	if (situ_walk_members(&image, shape, &first, &count) != SITU_WALK_OK) {
		return 1;
	}
	for (uint32_t i = 0; i < count; i++) {
		ASK
	}
	return 0;
}
"""

#: Print one value the way its own placement says to read it. A value comes
#: back sign-extended through a `uint64_t`, so `-2` and 18446744073709551614
#: are the same answer and only `SITU_WALK_SIGNED` says which -- printing it
#: unsigned reported a disagreement with the Python walk that was not there.
SHOW = """static void show(const situ_walk_placement *held, uint64_t value,
                 const char *end)
{
	if ((held->flags & SITU_WALK_SIGNED) != 0u) {
		printf("%lld%s", (long long)value, end);
	} else {
		printf("%llu%s", (unsigned long long)value, end);
	}
}
"""

#: The value read: what a caller asks a member for.
VALUES = """uint64_t value = 0;
		situ_walk_placement held;
		if (situ_walk_placement_at(&image, first + i, &held) != SITU_WALK_OK) {
			return 1;
		}
		if (situ_walk_read(&image, msg, len, shape, first + i, &value)
				== SITU_WALK_OK) {
			show(&held, value, "\\n");
		} else {
			printf("refused\\n");
		}"""

#: A run's elements, as `count:e0,e1,...`. The third quantity the walk
#: computes and the one a caller of a run actually wants: `situ_walk_read`
#: refuses a run because it has no single value, which says nothing about the
#: values it does have.
ELEMENTS = """uint32_t n = 0;
		situ_walk_placement held;
		if (situ_walk_placement_at(&image, first + i, &held) != SITU_WALK_OK) {
			return 1;
		}
		if (situ_walk_count(&image, msg, len, shape, first + i, &n)
				!= SITU_WALK_OK) {
			printf("refused\\n");
			continue;
		}
		printf("%u:", n);
		for (uint32_t e = 0; e < n; e++) {
			uint64_t one = 0;
			const char *end = (e + 1 == n) ? "" : ",";
			if (situ_walk_element(&image, msg, len, shape, first + i, e, &one)
					== SITU_WALK_OK) {
				show(&held, one, end);
			} else {
				printf("refused%s", end);
			}
		}
		printf("\\n");"""

#: The width, in bytes. A member the walk declines to *value* still has an
#: extent, and the extent is what places everything after it -- so a walker
#: can agree about every value in a struct and still disagree about the
#: struct. The delimiter scan is an answer this asks for directly rather than
#: inferring from the next member's offset, which says nothing about the
#: last member.
WIDTHS = """uint32_t bits = 0;
		if (situ_walk_size_bits(&image, msg, len, shape, first + i, &bits)
				== SITU_WALK_OK) {
			printf("%u\\n", bits / 8u);
		} else {
			printf("refused\\n");
		}"""

#: Where a member starts, in bytes. The width probe above cannot see this:
#: a chain that overshoots a short frame and one that stops at it produce the
#: same widths, and differ only in the offset they hand the member after.
OFFSETS = """uint32_t bits = 0;
		if (situ_walk_offset_bits(&image, msg, len, shape, first + i, &bits)
				== SITU_WALK_OK) {
			printf("%u\\n", bits / 8u);
		} else {
			printf("refused\\n");
		}"""

#: An endian marker's verdict: whether its field, read big-endian, equals the
#: `little` sentinel. A non-marker member refuses, which is the same shape as
#: every other probe -- what this build declines is part of what it says.
MARKERS = """uint32_t little = 0;
		if (situ_walk_marker(&image, msg, len, shape, first + i, &little)
				== SITU_WALK_OK) {
			printf("little=%u\\n", little);
		} else {
			printf("refused\\n");
		}"""

#: A tag's presence: whether its span is inside the frame. The caller runs
#: the verification algorithm; the walker only says the bytes are there, which
#: is exactly what `situ_walk_bytes` answers. A non-tag member refuses, so the
#: two walkers' lists line up member for member.
TAGS = """situ_walk_placement held;
		if (situ_walk_placement_at(&image, first + i, &held) != SITU_WALK_OK) {
			return 1;
		}
		if ((held.flags & SITU_WALK_IS_TAG) == 0u) {
			printf("refused\\n");
			continue;
		}
		const uint8_t *at = NULL;
		uint32_t       n  = 0;
		if (situ_walk_bytes(&image, msg, len, shape, first + i, &at, &n)
				== SITU_WALK_OK) {
			printf("present=1\\n");
		} else {
			printf("present=0\\n");
		}"""

#: A sealed region's gate, and the interior it opens. The gate's own verdict
#: does not depend on the bytes -- situ guards them, the caller runs the
#: cipher -- so `refused=1 opened=1` is constant; what is worth comparing is
#: the scalars *inside*, the half a tag exists to protect, read through the
#: gate. A non-gate member refuses, so the two walkers line up.
GATES = """uint32_t opened = 0, refused = 0;
		if (situ_walk_gate(&image, first + i, &opened, &refused)
				!= SITU_WALK_OK) {
			printf("refused\\n");
			continue;
		}
		printf("refused=%u opened=%u\\n", refused, opened);
		uint32_t ord = 0, inside = 0;
		while (situ_walk_gated(&image, first + i, ord, &inside)
				== SITU_WALK_OK) {
			uint64_t value = 0;
			situ_walk_placement ih;
			if (situ_walk_placement_at(&image, inside, &ih) != SITU_WALK_OK) {
				return 1;
			}
			if (situ_walk_read(&image, msg, len, shape, inside, &value)
					== SITU_WALK_OK) {
				show(&ih, value, "\\n");
			} else {
				printf("refused\\n");
			}
			ord++;
		}"""


def image_for(path: Path) -> bytes:
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return pack(schema, resolved)[0]


def shape_named(path: Path, name: str) -> int:
	"""The index of a struct in `image_for`'s blob, asked rather than counted.

	That blob is packed without metadata, so it carries no names; this packs
	the same schema WITH them and looks the name up. The index transfers,
	because metadata adds sections rather than reordering one.

	Written after a hard-coded 11 quietly became a different struct. A schema
	gained a struct above `constrained`, every index below it shifted, and
	the two walkers went on agreeing with each other perfectly -- what failed
	was only the expectation about WHICH struct was being asked, which is a
	wrong-population read wearing a disagreement's clothes.
	"""
	return shape_named_text(path.read_text(encoding="ascii"), name,
	                        str(path))


def shape_named_text(text: str, name: str, whence: str = "inline.situ") -> int:
	"""`shape_named` for a schema written into this file rather than on disk.

	The inline schemas here go through `packed_text`, which has no path to
	hand `shape_named`, and counting the structs by hand is the hard-coded 11
	above."""
	source   = Source(whence, text)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return load(pack(schema, resolved, metadata=True)[0]).struct_names.index(name)


def python_answers(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		try:
			found.append(str(read_scalar(view, index)))
		except Refused:
			found.append("refused")
	return found


def python_widths(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		try:
			found.append(str(size_bits(view, index) // 8))
		except (Refused, Unplaceable):
			found.append("refused")
	return found


def _drive(tmp_path: Path, blob: bytes, message: bytes, ask: str,
		shape: int = 0) -> list[str]:
	(tmp_path / "img").write_bytes(blob)
	(tmp_path / "drive.c").write_text(
		DRIVER.replace("SHOW", SHOW).replace("ASK", ask), encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{WALKER}", str(tmp_path / "drive.c"),
		 str(WALKER / "situ_walk.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"),
	                      message.hex(), str(shape)],
	                     capture_output=True, text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	return ran.stdout.split()


def c_answers(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, VALUES, shape)


def c_widths(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, WIDTHS, shape)


def c_offsets(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, OFFSETS, shape)


def python_offsets(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		try:
			found.append(str(offset_bits(view, index) // 8))
		except Refused:
			found.append("refused")
	return found


def c_markers(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, MARKERS, shape)


def c_tags(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, TAGS, shape)


def python_tags(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	"""A tag's presence, spelled the Python walker's way (report._members):
	`read_bytes` succeeds where the span is in the frame. A non-tag member
	refuses, matching the C driver, so the two lists line up member for
	member."""
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		if not image.placements[index].is_tag:
			found.append("refused")
			continue
		try:
			read_bytes(view, index)
			found.append("present=1")
		except (Refused, Unplaceable):
			found.append("present=0")
	return found


def c_gates(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, GATES, shape)


def python_gates(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	"""A sealed gate and its interior, the Python walker's way (report._gates
	and report._gated). The gate verdict is constant -- `refused=1 opened=1`,
	the claim every backend keeps -- and the interior scalars are read through
	it. A non-gate member refuses, so the two walkers line up member for
	member with the interior scalars spliced in after their gate."""
	image = load(blob)
	view  = acquire(image, message, shape)
	gates = set(report._gates(image, shape))
	found = []
	for index in image.members(image.structs[shape]):
		if index not in gates:
			found.append("refused")
			continue
		found.append("refused=1")
		found.append("opened=1")
		for inside in report._gated(image, index):
			try:
				found.append(str(read_scalar(view, inside)))
			except (Refused, Unplaceable):
				found.append("refused")
	return found


def python_markers(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	"""The marker verdict, spelled the Python walker's way (report._members).

	Read big-endian whatever the marker says -- it decides the byte order, so
	it cannot be read in the order it is about -- and compared against the
	`little` sentinel. A non-marker member refuses, matching the C driver's
	UNSUPPORTED, so the two lists line up member for member.
	"""
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		if index not in image.markers:
			found.append("refused")
			continue
		try:
			start = view.at + offset_bits(view, index) // 8
			width = size_bits(view, index) // 8
			if start + width > view.limit:
				raise Refused("the frame does not reach the marker")
			held = int.from_bytes(view.buffer[start:start + width], "big")
			found.append(f"little={1 if held == image.markers[index] else 0}")
		except (Refused, Unplaceable):
			found.append("refused")
	return found


def c_elements(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> list[str]:
	return _drive(tmp_path, blob, message, ELEMENTS, shape)


def python_elements(blob: bytes, message: bytes, shape: int = 0) -> list[str]:
	"""The same question of the Python walk, spelled its way.

	`report._element` is where the fifth column reads one, and `walk._count`
	is what says how many -- the two the C API had no equivalent of until a
	run was more than a span to skip over.
	"""
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		placement = image.placements[index]
		try:
			if placement.radix or placement.repeat_code != NONE:
				raise Refused("not a counted run")
			count = walk._count(view, index)
		except (Refused, Unplaceable):
			found.append("refused")
			continue
		held = []
		for at in range(count):
			try:
				held.append(str(report._element(view, index, at)))
			except (Refused, Unplaceable):
				held.append("refused")
		found.append(f"{count}:" + ",".join(held))
	return found


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_it_compiles_under_the_same_warnings(tmp_path: Path) -> None:
	"""An embedded walker needing a relaxed warning set is one nobody can
	put in a build."""
	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, "-c", str(WALKER / "situ_walk.c"),
		 "-o", str(tmp_path / "o.o")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_it_agrees_with_the_python_walker(tmp_path: Path) -> None:
	"""Two independent readers of one image over the same bytes.

	Including the refusals: what this build declines is part of what it
	says, and a disagreement about *that* is as real as one about a value.
	"""
	blob    = image_for(ROOT / "example" / "udp" / "udp.situ")
	message = bytes.fromhex("1f90238200105f2a")

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)


_MARKER_SCHEMA = """target buffer;

endian_marker order : u16 {
	little = 0x4949,
	big    = 0x4D4D,
}

struct m [endian = from(order)] {
	endian_marker  order;
	u16            x;
}
"""


def _inline_image(text: str) -> bytes:
	source   = Source("marker.situ", text)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return pack(schema, resolved)[0]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_an_endian_marker(tmp_path: Path) -> None:
	"""The marker decides the message's byte order, so both walkers read it
	big-endian and ask whether it equals the `little` sentinel -- the one
	probe whose answer cannot depend on the answer. A non-marker member
	refuses in both, which is part of what agreement means (0035)."""
	blob = _inline_image(_MARKER_SCHEMA)

	little = bytes.fromhex("49490500")	# order = II, then x
	big    = bytes.fromhex("4d4d0500")	# order = MM

	assert c_markers(tmp_path, blob, little) == python_markers(blob, little)
	assert c_markers(tmp_path, blob, little) == ["little=1", "refused"]
	assert c_markers(tmp_path, blob, big) == python_markers(blob, big)
	assert c_markers(tmp_path, blob, big) == ["little=0", "refused"]


_DIGITS_SCHEMA = """target buffer;
endian big;

struct s {
	hex u32  n[4];
	u8       body[n];
	u16      tail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_a_bad_digit_sizes_a_member_at_zero(
		tmp_path: Path) -> None:
	"""A text number has two readers, and an expression wants the lax one.

	Every backend emits both: `_get` returns an error when the bytes are
	not digits, and `_value` cannot fail and yields zero. A length
	expression calls `_value` -- the next member has to be placed whether
	or not the field parsed -- and `validate` calls `_get`, which is where
	a field that is not a number is called malformed.

	This walker read both through the strict one, so a driver holding
	`ZZZZ` refused the length and every member after it went with it.
	`walk.py` carried the same fault and its `_value_of` records the fix;
	this is the second implementation of that module and had not inherited
	it (26.271).

	Both digit strings are asserted. A walker that sized everything at
	zero would pass the bad one on its own -- and the valid case is what
	says the lax reader still reads.
	"""
	blob = _inline_image(_DIGITS_SCHEMA)

	good = b"0006" + b"aaaaaa" + b"\xbe\xef"
	bad  = b"ZZZZ" + b"aaaaaa" + b"\xbe\xef"

	assert python_widths(blob, good) == ["4", "6", "2"]
	assert c_widths(tmp_path, blob, good) == ["4", "6", "2"]
	assert python_widths(blob, bad) == ["4", "0", "2"]
	assert c_widths(tmp_path, blob, bad) == ["4", "0", "2"]


_NEGATIVE_SCHEMA = """target buffer;
endian big;

struct s {
	u8   n  [min = 5];
	u8   body[(n - 5) * 4];
	u16  tail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_a_negative_length_reads_as_zero(
		tmp_path: Path) -> None:
	"""14.2b: a length expression that goes negative reads as zero.

	Every other description does it. The generated C spells it
	`situ_nonneg_u32`, and `walk.py` clamps with `min(max(count, 0), ...)`
	under a comment saying the clamp is there "to agree with the three
	backends that do rather than for its own sake". This walker returned
	BOUNDS instead, so `example/tcp`'s `options[(data_offset - 5) * 4]` --
	whose own schema comment says a negative "reads as zero rather than as
	a length" -- was refused here and answered 0 by the other five, taking
	every member after it with it.

	Both `n` values are asserted, because a walker that answered 0 for
	everything would pass the negative case alone.

	`[min = 5]` is what makes the schema legal -- without it the solver
	refuses the member outright, "array length [-20, 1000] may be
	negative". The constraint bounds the range at COMPILE time and a
	message can still carry less, which is the whole reason 14.2b has
	something to say: `validate` reports such a message as malformed and
	the accessor still has to answer. `example/tcp` declares
	`u4 data_offset [min = 5]` for the same reason.
	"""
	blob = _inline_image(_NEGATIVE_SCHEMA)

	short = bytes.fromhex("02") + b"\xbe\xef"          # n = 2, so (2-5)*4 < 0
	long_ = bytes.fromhex("06") + b"aaaa" + b"\xbe\xef"  # n = 6, so 4 bytes

	assert python_widths(blob, short) == ["1", "0", "2"]
	assert c_widths(tmp_path, blob, short) == ["1", "0", "2"]
	assert python_widths(blob, long_) == ["1", "4", "2"]
	assert c_widths(tmp_path, blob, long_) == ["1", "4", "2"]


_BITS_SCHEMA = """target buffer;
endian big;
bit_order BITORDER;

struct s {
	u3  low;
	u5  high;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_bit_packed_field(tmp_path: Path) -> None:
	"""A field that starts or ends inside a byte, from both ends.

	This walker refused every one of them. The guard tested ALIGNMENT --
	`start_bits % 8u || width_bits % 8u` -- while the comment above it
	argued that the solver will not place a bit-packed field at a DYNAMIC
	offset. Both sentences are true and the second does not license the
	first, so tcp's flags, dnsname's `u2 form`, and the same fields in
	ipv4, dns, ntp, mqtt and rtc were declined along with everything a
	variant of theirs selects.

	The numbers are `walk.py`'s own, from the comment recording the day it
	learned to consult `bit_order`: "for `u3 low; u5 high;` over `0xAB`
	under `lsb_first` it answers 3 and 21 where this answered 5 and 11".
	Pinning them rather than only comparing the two readers is what makes
	this a test of the values instead of a test that both were changed
	together -- and they come from a specification neither reader wrote.
	"""
	for order, expected in (("msb_first", ["5", "11"]),
	                        ("lsb_first", ["3", "21"])):
		blob = _inline_image(_BITS_SCHEMA.replace("BITORDER", order))
		message = bytes.fromhex("ab")

		assert python_answers(blob, message) == expected, order
		assert c_answers(tmp_path, blob, message) == expected, order


_NESTED_SCHEMA = """target buffer;
endian big;

struct inner {
	u8  n;
	u8  body[n];
}

struct outer {
	inner  head;
	u16    tail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_nested_variable_struct(tmp_path: Path) -> None:
	"""A member whose type is a struct with no single size.

	It carries no length program, because its extent is the sum of its own
	members and only `struct_extent` knows it. This walker fell through to
	the placement's `size_bits`, which is the MINIMUM -- and a minimum is
	not a refusal, so it answered. `head` came out one byte instead of
	four, `tail` was read at offset 1 instead of 4, and 0x6162 was
	reported as its value: inside the frame, so nothing complained.

	`walk.py` carries this same fix and the comment recording the same
	symptom on dnsname's `qname` -- one byte where it is seventeen, with
	`qtype` read at 1. Two independent readers of one image, and only one
	of them had been corrected.

	The widths are pinned as well as compared. Agreement alone would hold
	if both answered the minimum, which is exactly the state this came
	from.
	"""
	blob    = _inline_image(_NESTED_SCHEMA)
	message = bytes.fromhex("03616263beef")	# n=3, "abc", then 0xbeef

	assert python_widths(blob, message, 1) == ["4", "2"]
	assert c_widths(tmp_path, blob, message, 1) == ["4", "2"]

	# `tail` read where the nested struct ends rather than where its
	# minimum would have put it. 0x6162 was the old answer, and it is the
	# one worth pinning: agreement on 0xbeef is what the offset being right
	# looks like from outside.
	assert c_answers(tmp_path, blob, message, 1)[1] == "48879"
	assert python_answers(blob, message, 1)[1] == "48879"

	# The two still differ about the struct-typed member ITSELF -- C reads
	# one byte and answers 3, `walk.py` reads four and answers 0x03616263 --
	# and that is a separate path from the extent this fixes, unchanged by
	# it and disagreeing before it. Asserted here so the gap is recorded
	# where somebody comparing the two will meet it, rather than left for
	# the next reader to rediscover as a surprise.
	assert c_answers(tmp_path, blob, message, 1)[0] == "3"
	assert python_answers(blob, message, 1)[0] == "56713827"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_whether_a_tag_is_present(tmp_path: Path) -> None:
	"""A tag's presence is whether its span is inside the frame -- the caller
	runs the verification, the walker only says the bytes are there. Both read
	that from the same place, so a whole header answers `present=1` and one cut
	short of the checksum answers `present=0`, member for member (0035).

	`ipv4_header`'s `header_checksum` sits at byte 10 (two bytes) and is the
	tag; the shape is struct 1."""
	blob  = image_for(ROOT / "example" / "ipv4" / "ipv4.situ")
	whole = bytes(range(20))		# reaches the checksum at 10..11
	short = bytes(range(6))			# stops before it

	assert c_tags(tmp_path, blob, whole, shape=1) == python_tags(blob, whole, 1)
	assert c_tags(tmp_path, blob, short, shape=1) == python_tags(blob, short, 1)
	assert "present=1" in c_tags(tmp_path, blob, whole, shape=1)
	assert "present=0" in c_tags(tmp_path, blob, short, shape=1)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_sealed_gate_and_its_interior(
		tmp_path: Path) -> None:
	"""A sealed region's gate answers `refused=1 opened=1` -- situ guards the
	bytes and the caller runs the cipher, so the verdict does not depend on
	the bytes -- and its interior scalars are read through the gate, the half
	a tag protects. Both walkers agree member for member, the interior spliced
	in after the gate, and every non-gate member refuses (0035, 14.3).

	`packet`'s `sealed(aes_gcm_128, ...)` region is struct 1's fifth member;
	`inner_kind` (u16) and `inner_seq` (u32) sit at its start."""
	blob    = image_for(ROOT / "example" / "packet" / "packet.situ")
	message = bytes(range(48))		# reaches the region interior and the tag

	assert (c_gates(tmp_path, blob, message, shape=1)
	        == python_gates(blob, message, 1))
	got = c_gates(tmp_path, blob, message, shape=1)
	assert "refused=1" in got and "opened=1" in got


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_versioned_member(tmp_path: Path) -> None:
	"""A `[since]` member is there only in a message whose own version reaches
	it, and `validate` reads that version first and skips the member below it
	(0035). This build used to refuse every struct carrying one; now it reads
	the version field the packer names per shape and gates on it, member for
	member with the Python walk.

	`edges`' `constrained [version = rev]` is the case, looked up by name
	rather than numbered (`shape_named`): `rev` at byte
	zero, then `magic [since = 2, must_eq = 0x1234]`, `how [since = 3]` and
	`pad [since = 3, must_eq = 0]`. The endianness is `edges`' own, big. The
	gate has to *gate*: a v1 message whose later bytes would fail `magic`'s
	`must_eq` is well-formed, because at version one there is no `magic` to
	check -- and a walker that read it anyway would answer CONSTRAINT for a
	field the message never claimed to carry.
	"""
	edges = ROOT / "test" / "schema" / "edges.situ"
	blob  = image_for(edges)
	shape = shape_named(edges, "constrained")

	# (message, expected verdict): 0 OK, 2 CONSTRAINT.
	cases = [
		(bytes.fromhex("01"),           "0"),  # v1: magic/how/pad all excluded
		(bytes.fromhex("01ffff"),       "0"),  # v1: bad `magic` bytes, but it is excluded
		(bytes.fromhex("02ffff"),       "2"),  # v2: `magic` included and wrong
		(bytes.fromhex("021234"),       "0"),  # v2: `magic` = 0x1234
		(bytes.fromhex("0312340100"),   "0"),  # v3: magic ok, how=plain, pad=0
		(bytes.fromhex("03123401ff"),   "2"),  # v3: `pad` must be zero
		(bytes.fromhex("0312340900"),   "2"),  # v3: `how` is not a known dialect
	]
	for message, want in cases:
		verdict = c_verdict(tmp_path, blob, message, shape=shape)
		assert verdict == python_verdict(blob, message, shape=shape), message.hex()
		assert verdict == want, message.hex()


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_variable_member_is_refused_rather_than_guessed(
		tmp_path: Path) -> None:
	"""udp's payload has no constant extent, and this build says so. A
	number here would be a wrong length that reads like a right one.

	The scalars ahead of it are the control: they are read, so the refusal
	is about the payload rather than about the walk giving up. The checksum
	between them is a two-byte run and `read_scalar` refuses a run whatever
	the frame -- a checksum carries a length by construction, so there is no
	spelling of it that is a scalar, and asserting otherwise would tie this
	test to a shape `example/udp` is free to change.
	"""
	blob = image_for(ROOT / "example" / "udp" / "udp.situ")

	answers = c_answers(tmp_path, blob, bytes.fromhex("1f90238200105f2a"))

	assert answers[-1] == "refused"
	# The three scalars: ports and length, all inside the covered region and
	# all readable, which is what says the walk reached them at all.
	assert answers[:3] == ["8080", "9090", "16"]


VARIABLE = """target buffer;
endian big;

struct label {
	u16 id;
	u8  n;
	u8  name[n];
	u8  tail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_where_the_data_decides_a_length(tmp_path: Path) -> None:
	"""The case that once disagreed, and the reason the claim is no longer
	narrowed to schemas whose members are all scalars.

	Python's `read_scalar` answered a byte run -- "hello" came back as
	448378203247 -- and the C walker refused it. `read_scalar` refuses one
	now, so both decline the run and both place `tail` *after* it, which is
	the offset chain working in two independent implementations.
	"""
	source   = parse(Source("var.situ", VARIABLE))
	resolved = resolve(source, solve(source))
	blob     = pack(source, resolved)[0]
	message  = bytes.fromhex("12340568656c6c6f7f")

	answers = c_answers(tmp_path, blob, message)

	assert answers == python_answers(blob, message)
	assert answers == ["4660", "5", "refused", "127"]


VARINT = """target buffer;
endian big;

varint_type small {
	encoding  = leb128;
	max_bits  = 32;
	max_bytes = 5;
}

struct counted {
	u8    lead;
	small n;
	u8    after;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_what_a_varint_holds(tmp_path: Path) -> None:
	"""Both walkers read the bytes rather than the value, and neither said so.

	Python answered `ac 02` as 44034, those two bytes as an integer; the C
	side answered 172, the first of them, because a varint's record carries
	the one-byte lower bound as its width. Both decode now, which is what
	every compiled backend's `_get` does.

	The bytes matter to this test. `96 01` is 150 decoded and 0x96 is 150
	raw, so a walker reading one byte and a walker decoding two agree on it
	by coincidence -- and that pair is what the divergence was first written
	up from, which had the C side down as already correct. Nothing here uses
	a number two readings can produce.
	"""
	source   = parse(Source("varint.situ", VARINT))
	resolved = resolve(source, solve(source))
	blob     = pack(source, resolved)[0]
	message  = bytes.fromhex("11ac0222")

	answers = c_answers(tmp_path, blob, message)

	assert answers == python_answers(blob, message)
	assert answers == ["17", "300", "34"]


WIDE = """target buffer;
endian big;

varint_type wide {
	encoding  = be128;
	max_bits  = 64;
	max_bytes = 9;
}

struct counted {
	u8   lead;
	wide n;
	u8   after;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_the_other_varint_encoding(tmp_path: Path) -> None:
	"""`be128` takes the high group first -- ASN.1's identifier octets and
	SQLite's record varints -- and is the other byte order rather than the
	same one spelled differently. `81 00` holds 128 there and 33024 read as
	raw bytes, so the two encodings are told apart by this and not only the
	decode from the raw read."""
	source   = parse(Source("wide.situ", WIDE))
	resolved = resolve(source, solve(source))
	blob     = pack(source, resolved)[0]
	message  = bytes.fromhex("11810022")

	answers = c_answers(tmp_path, blob, message)

	assert answers == python_answers(blob, message)
	assert answers == ["17", "128", "34"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_a_truncated_varint_has_no_value(
		tmp_path: Path) -> None:
	"""The width answers zero and the value refuses, in both.

	Two readers, as for a text number: the generated `_len` gives a truncated
	varint no bytes and goes on placing what follows, because the length
	arithmetic downstream of it is not fallible, and only `_get` refuses. The
	C walker refused the width and so dropped `after` out of a struct four
	backends read to the end -- the same rule Python learned in 26.94, learned
	again by the implementation written after it was recorded.

	`after` is asserted, not just the refusal: it is the whole of what the lax
	reader buys, and a walker that refuses the width passes a test that only
	looks at the varint.
	"""
	source   = parse(Source("varint.situ", VARINT))
	resolved = resolve(source, solve(source))
	blob     = pack(source, resolved)[0]
	message  = bytes.fromhex("11ac")

	answers = c_answers(tmp_path, blob, message)

	assert answers == python_answers(blob, message)
	assert answers == ["17", "refused", "172"]


DELIMITED = """target buffer;
endian big;

struct line {
	u8  verb[] until " " max 8;
	u16 code;
}
"""

QUOTED = """target buffer;
endian big;

struct row {
	u8  field[] until "," max 16 [quoted = "\\""];
	u16 after;
}
"""

ESCAPED = """target buffer;
endian big;

struct row {
	u8  field[] until "," max 16 [escape = "\\\\"];
	u16 after;
}
"""


def packed_text(text: str) -> bytes:
	source   = parse(Source("scan.situ", text))
	resolved = resolve(source, solve(source))
	return pack(source, resolved)[0]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_where_a_delimiter_decides_the_end(tmp_path: Path) -> None:
	"""The third construct to reach the scalar read as bits, and the second
	settled by refusing.

	Python answered `"GET "` as 1195725856, the span read as an integer; the
	C walker had no scan at all, so it took the record's `size_bits` -- which
	for a delimited member is its *delimiter's* width, the one number that is
	not the answer -- and gave 0x47 for the member and offset 1 for the `u16`
	after it, where it belongs at 4.

	Neither answers a value now, and the widths are compared rather than
	inferred: 4 is the content plus the delimiter, which is what places
	`code`.
	"""
	blob    = packed_text(DELIMITED)
	message = b"GET \x00\xff"

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["refused", "255"]
	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["4", "2"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_unterminated_member_reaches_its_cap(
		tmp_path: Path) -> None:
	"""A delimiter that never arrives leaves the member truncated, not
	unplaceable: it reaches as far as `max` allowed and `code` begins there.
	Refusing instead drops every member after it, which is not what any
	backend does -- and the delimiter is not part of the member when it is
	not there, so the width is 8 rather than 9."""
	blob    = packed_text(DELIMITED)
	message = b"GETTINGX\x00\xff"

	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["8", "2"]
	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["refused", "255"]


SKIPPED = """target buffer;
endian big;
whitespace ' ' | '\\t';

struct counted {
	u8  a;
	u8  b  skip;
	u8  c;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_where_whitespace_precedes_a_member(tmp_path: Path) -> None:
	"""A lead is the newest place two readers of one image can come apart,
	and it is the first construct that moves a member's OWN offset rather
	than the offsets after it.

	Both halves are asserted because either alone passes with the lead
	counted twice: the value says the member was found past the whitespace,
	and the width says the whitespace was charged to it exactly once. A
	walker that added the lead to the offset and not to the span would
	answer `b` correctly and place `c` on top of it."""
	blob    = packed_text(SKIPPED)
	message = b"a  \tbc"

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["97", "98", "99"]
	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["1", "4", "1"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_absent_lead_costs_nothing(tmp_path: Path) -> None:
	"""The control for the case above, and the one that separates "the lead
	was read" from "the lead was assumed": the same schema over bytes with
	no whitespace in them must give the same answers a schema without
	`skip` would."""
	blob    = packed_text(SKIPPED)
	message = b"abc"

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["97", "98", "99"]
	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["1", "1", "1"]


TEXT_FIXED = """target buffer;
endian big;

struct counted {
	decimal u32  n[4];
	u8           after;
}
"""

TEXT_HEX = """target buffer;
endian big;

struct counted {
	hex u32  n[4];
	u8       after;
}
"""

TEXT_DELIMITED = """target buffer;
endian big;

struct counted {
	decimal u32  n[] until " " max 4;
	u8           after;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_text_number(tmp_path: Path) -> None:
	"""The last construct in the bits-versus-values pattern, and the one the
	C walker was refusing for the wrong reason.

	`decimal u32 n[4]` is one number in four digits, so the run refusal
	declined it -- a true answer drawn from a false premise, since the digit
	count is not a count of numbers. It parses now, in both radices, and the
	upper and lower case of a hex digit are one number.

	The widths are asserted because that is where the second bug was.
	`size_code` is set on a fixed-width text number, so the sized-run branch
	read `[4]` as four 32-bit elements and answered sixteen bytes -- the same
	arithmetic that once put `edges`' `text_driver` tail twelve bytes past
	where every backend places it. It was invisible through the values alone:
	the solver hands a member after a fixed-width text number a constant
	offset, so `after` was read correctly out of a struct measured four times
	too long.
	"""
	for text, message, expected, widths in (
			(TEXT_FIXED,     b"0123\xff", ["123", "255"],     ["4", "1"]),
			(TEXT_FIXED,     b"12x4\xff", ["refused", "255"], ["4", "1"]),
			(TEXT_HEX,       b"00ff\xff", ["255", "255"],     ["4", "1"]),
			(TEXT_HEX,       b"00FF\xff", ["255", "255"],     ["4", "1"]),
			(TEXT_DELIMITED, b"250 \xff", ["250", "255"],     ["4", "1"]),
			(TEXT_DELIMITED, b"7 \xff",   ["7", "255"],       ["2", "1"]),
	):
		blob = packed_text(text)

		assert c_answers(tmp_path, blob, message) \
			== python_answers(blob, message), message
		assert c_answers(tmp_path, blob, message) == expected, message
		assert c_widths(tmp_path, blob, message) \
			== python_widths(blob, message), message
		assert c_widths(tmp_path, blob, message) == widths, message


RUNS = {
	"a counted run":
		("struct s { u16 xs[3]; u8 tail; }",
		 bytes.fromhex("0001000200037f"), ["3:1,2,3", "refused"]),
	"a run the message sizes":
		("struct s { u8 n; u16 xs[n]; u8 tail; }",
		 bytes.fromhex("02000100027f"), ["refused", "2:1,2", "refused"]),
	"a run the message sizes to nothing":
		("struct s { u8 n; u16 xs[n]; u8 tail; }",
		 bytes.fromhex("007f"), ["refused", "0:", "refused"]),
	"a signed element":
		("struct s { i16 xs[2]; u8 tail; }",
		 bytes.fromhex("fffe00027f"), ["2:-2,2", "refused"]),
	"a byte run":
		("struct s { u8 n; u8 xs[n]; u8 tail; }",
		 bytes.fromhex("034142437f"), ["refused", "3:65,66,67", "refused"]),
}


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_run_element_by_element(tmp_path: Path) -> None:
	"""What a run holds, which `situ_walk_read` refusing one says nothing
	about.

	A run has no single value and both walkers decline to give it one; the
	values it *does* have needed an accessor, and the C walker had none --
	so a device could be told a run is fourteen bytes long and had no way to
	read any of them. `situ_walk_count` and `situ_walk_element` are that,
	and the element read is the scalar read at a different offset rather
	than a second spelling of it.

	The signed case earns its place. A value comes back sign-extended
	through a `uint64_t`, so `-2` and 18446744073709551614 are the same
	answer, and the walker had no way for a caller to ask which it was
	looking at -- this test printed one unsigned and reported a
	disagreement that was not there. `SITU_WALK_SIGNED` is named in the
	header now, which is what a missing accessor costs when it is only
	found from outside.

	A run of zero elements is a run, not a refusal: `n = 0` answers `0:`
	in both, and nothing after it moves.
	"""
	for label, (body, message, expected) in RUNS.items():
		blob = packed_text(f"target buffer;\nendian big;\n\n{body}\n")

		assert c_elements(tmp_path, blob, message) \
			== python_elements(blob, message), label
		assert c_elements(tmp_path, blob, message) == expected, label


#: `validate`'s verdict, or `cannot-say` where this build declines to answer
#: for the struct at all. The two are different questions: the verdict is
#: about the message and the refusal is about the walker, and folding them
#: together would report a struct whose rules are not carried as well-formed.
VERDICT = """situ_walk_err verdict = SITU_WALK_OK;
		const situ_walk_err e = situ_walk_validate(&image, msg, len, shape,
		                                           &verdict);
		if (i > 0) {
			continue;	/* one answer per struct, not per member */
		}
		printf("%s\\n", e != SITU_WALK_OK ? "cannot-say"
		       : (verdict == SITU_WALK_OK ? "0"
		          : (verdict == SITU_WALK_BOUNDS ? "1" : "2")));"""


def c_verdict(tmp_path: Path, blob: bytes, message: bytes,
		shape: int = 0) -> str:
	return _drive(tmp_path, blob, message, VERDICT, shape)[0]


def python_verdict(blob: bytes, message: bytes, shape: int = 0) -> str:
	"""The same question of the fifth column.

	A frame too short for the struct's own minimum is refused when the view
	is *acquired* here and reached by placing members there, so the one
	raises and the other answers BOUNDS. Same check, same verdict, different
	place -- named rather than papered over, because it is the only
	structural difference between the two validators.
	"""
	image = load(blob)
	try:
		view = acquire(image, message, shape)
	except Refused:
		return "1"
	answer = report._validate(image, view, shape)
	return "cannot-say" if answer is None else str(answer)


#: Each is `hdr` plus whatever it needs, and the messages are chosen so that
#: every verdict appears: well-formed, a rule broken, and a frame too short.
VALIDATED = {
	"a constrained header": (
		"struct hdr { u16 magic [must_eq = 0x1234]; u8 n [min = 1, max = 4];"
		" u8 pad [must_eq = 0]; u16 tail; }",
		[("12340200beef", "0"), ("99990200beef", "2"), ("12340900beef", "2"),
		 ("12340000beef", "2"), ("12340207beef", "2"), ("123402", "1")]),
	"an enum that rejects the unknown": (
		"enum kind : u8 { a = 0x11, b = 0x22, default = error, }\n"
		"struct hdr { kind k; u16 tail; }",
		[("1100ff", "0"), ("9900ff", "2"), ("11", "1")]),
	"a nested struct's own rules": (
		"struct inner { u16 m [must_eq = 1]; }\n"
		"struct hdr { inner i; u16 tail; }",
		[("0001beef", "0"), ("0002beef", "2"), ("00", "1")]),
	"a text number in range": (
		"struct hdr { decimal u16 n[3] [max = 500]; u8 tail; }",
		[("313233ff", "0"), ("393939ff", "2"), ("3132ff", "1"),
		 ("3132ffff", "2")]),
	# The checks below read a member's *span* rather than its value, which
	# is what separates them from everything above.
	"a nul terminator that has to be there": (
		"struct hdr { u8 name[4] [nul_terminated]; u8 tail; }",
		[("41420000ff", "0"), ("41424344ff", "2")]),
	"bytes that have to be ASCII": (
		"struct hdr { u8 name[4] [nul_terminated, encoding = ascii];"
		" u8 tail; }",
		[("41420000ff", "0"), ("41ff0000ff", "2")]),
	# UTF-8 is the one that could be got subtly wrong rather than plainly:
	# a bad continuation byte and a surrogate half both look like text.
	"bytes that have to be UTF-8": (
		"struct hdr { u8 name[4] [nul_terminated, encoding = utf8];"
		" u8 tail; }",
		[("c3a90000ff", "0"), ("c3280000ff", "2"), ("eda08000ff", "2")]),
	"a reserved run that has to be zero": (
		"struct hdr { u8 v; reserved u8[2] [must_be_zero]; u8 tail; }",
		[("010000ff", "0"), ("010100ff", "2")]),
	# `[remaining]` is here because it is what caught the walker measuring
	# it from the start of the buffer rather than from the member.
	"a delimiter that has to be there": (
		'struct hdr { u8 verb[] until " " max 4; u8 rest[remaining]; }',
		[("47455420ff", "0"), ("47455454ff", "2"), ("4720ffffff", "0")]),
}


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_whether_a_message_is_well_formed(
		tmp_path: Path) -> None:
	"""`validate` in C, held to the fifth column.

	This is the answer a device wants and could not get: the walker could
	say where every member is and what it holds, and not whether the message
	is a legal instance of the schema.

	Whole or nothing, which is the part that needed building rather than
	porting. Every other probe renders per member and skips what it cannot
	do; `validate` is one verdict about a whole struct, so a partial one
	reports OK for the rules it happened to be given. The image carries a
	bit per struct saying it holds every check, and a kind of check this
	build does not render refuses the *struct* rather than skipping the
	member.

	The messages cover each verdict for each shape, because a validator that
	has only seen well-formed input has not been asked anything -- and the
	short frames are there for the one structural difference between the two
	validators, which `python_verdict` names.
	"""
	for label, (body, cases) in VALIDATED.items():
		blob, image = _packed_named(f"target buffer;\nendian big;\n\n{body}\n")
		shape = [image.struct_name(i)
		         for i in range(len(image.structs))].index("hdr")

		for hexed, expected in cases:
			message = bytes.fromhex(hexed)
			assert c_verdict(tmp_path, blob, message, shape) \
				== python_verdict(blob, message, shape), f"{label}: {hexed}"
			assert c_verdict(tmp_path, blob, message, shape) == expected, \
				f"{label}: {hexed}"


def _packed_named(text: str) -> tuple[bytes, Image]:
	"""An image with its metadata tail, so a struct can be found by name."""
	source   = parse(Source("named.situ", text))
	resolved = resolve(source, solve(source))
	blob, _  = pack(source, resolved, metadata=True)
	return blob, load(blob)


#: Deliberately without `[equalize]`. Padding every arm to the largest is
#: what buys back the offset axis, and it would also hide a walk that picked
#: the wrong arm: every answer would be right by construction.
VARIANT = """target buffer;
endian big;

enum kind : u8 {
	small = 0x11,
	large = 0x22,
	default = error,
}

struct sized {
	kind  which;
	variant body switch (which) {
		case kind.small: u8  a[2];
		case kind.large: u8  b[8];
	}
	u16  tail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_the_arm_a_discriminant_selects(
		tmp_path: Path) -> None:
	"""A variant's extent is a switch, not the minimum and not the worst
	case.

	Reading the minimum instead is what made a dnsname label one byte long
	and walked thirty-nine of them through a thirty-eight byte buffer. The
	arms here are two bytes and eight, so `tail` lands at 3 or at 9 and a
	walk that took either the smallest or the largest would be caught by one
	of the two messages.

	An unrecognised discriminant is nought bytes rather than a refusal: that
	is a malformed message and saying so is `validate`'s job, not the
	extent's -- the generated C has the same `: 0u`.

	And the *value* is refused in both, which it was not. Python read the
	selected arm's bytes as an integer, so the two-byte arm came back as
	43707 -- the fifth construct to reach the scalar read as bits, and the
	third settled by refusing. A variant is a shape the discriminant chooses;
	the arm is what holds a value, and it has its own placement.
	"""
	source   = parse(Source("variant.situ", VARIANT))
	resolved = resolve(source, solve(source))
	blob, _  = pack(source, resolved, metadata=True)
	image    = load(blob)
	shape    = [image.struct_name(i)
	            for i in range(len(image.structs))].index("sized")

	for message, widths, values in (
			(bytes.fromhex("11aabbbeef"),
			 ["1", "2", "2"], ["17", "refused", "48879"]),
			(bytes.fromhex("220011223344556677beef"),
			 ["1", "8", "2"], ["34", "refused", "48879"]),
			(bytes.fromhex("99aabbbeef"),
			 ["1", "0", "2"], ["153", "refused", "43707"]),
	):
		assert c_widths(tmp_path, blob, message, shape) \
			== python_widths(blob, message, shape), message.hex()
		assert c_widths(tmp_path, blob, message, shape) == widths, \
			message.hex()
		assert c_answers(tmp_path, blob, message, shape) \
			== python_answers(blob, message, shape), message.hex()
		assert c_answers(tmp_path, blob, message, shape) == values, \
			message.hex()


#: `beats` from `test/schema/edges.situ`, whose header says the walk is
#: there so a termination bug in it has somewhere to show.
WHILE_RUN = """target buffer;
endian big;

struct beat {
	u8  kind;
	u8  payload;
}

struct beats {
	beat  pulse[] while (kind == 0x33) max 6;
	u16   after;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_where_a_while_run_stops(tmp_path: Path) -> None:
	"""A run that ends at whichever element first fails the predicate.

	Four ways to stop and a case for each, because every guard in that loop
	is there for an adversary who picks the bytes:

	  - the predicate goes false, which is the construct's own reason;
	  - the frame runs out mid-run, and the elements that fit still count;
	  - the cap is reached, `max 6` over nine elements' worth of input;
	  - and the first element already fails, which is one element and not
	    zero -- `while` asks about the element just parsed, which is the
	    whole difference from `until`.

	The struct is named rather than assumed. `beats` is the container and
	`beat` is the element, and the packer put the element first -- so a
	harness that always walked struct 0 was measuring the wrong struct and
	agreeing with itself about it.
	"""
	source   = parse(Source("beats.situ", WHILE_RUN))
	resolved = resolve(source, solve(source))
	blob, _  = pack(source, resolved, metadata=True)
	image    = load(blob)
	shape    = [image.struct_name(i)
	            for i in range(len(image.structs))].index("beats")

	for message, widths in (
			(bytes.fromhex("3301330233ffbeef"),                 ["8", "2"]),
			(bytes.fromhex("4401beef"),                         ["2", "2"]),
			(bytes.fromhex("33013302"),                         ["4", "2"]),
			(bytes.fromhex("33013302330333043305330633073308beef"),
			                                                    ["12", "2"]),
	):
		assert c_widths(tmp_path, blob, message, shape) \
			== python_widths(blob, message, shape), message.hex()
		assert c_widths(tmp_path, blob, message, shape) == widths, \
			message.hex()


LOCATED = """target buffer;
endian big;

struct s {
	u8   where;
	u8   pad;
	u16  tail at where;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_about_a_member_that_says_where_it_is(
		tmp_path: Path) -> None:
	"""`at expr`: a member that joins no offset chain.

	The C walker refused one outright and the offset is the whole of the
	construct, so the program answers it. Two messages rather than one,
	because a located member whose expression happens to land where the chain
	would have put it is a member that proves nothing: `where = 4` reads the
	`u16` at 4, and `where = 2` reads it at 2, overlapping `pad`.

	Overlapping deliberately. `at` is what a format uses when a header
	declares an offset, and nothing says the regions it names are disjoint --
	bmp's `pixels at file.pixel_offset` is the case in the tree.
	"""
	blob = packed_text(LOCATED)

	for message, expected in ((bytes.fromhex("0400deadbeef"),
	                           ["4", "0", "48879"]),
	                          (bytes.fromhex("02aabbcc"),
	                           ["2", "170", "48076"])):
		assert c_answers(tmp_path, blob, message) \
			== python_answers(blob, message), message.hex()
		assert c_answers(tmp_path, blob, message) == expected, message.hex()


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_a_quoted_delimiter_is_content(tmp_path: Path) -> None:
	"""8.6.1's other half: inside a quoted run the delimiter is data. `"a,b",`
	ends at the fifth byte and not the second, so the member is six bytes with
	its delimiter and `after` starts there."""
	blob    = packed_text(QUOTED)
	message = b'"a,b",\x00\xff'

	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["6", "2"]
	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["refused", "255"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_escaped_delimiter_is_content(
		tmp_path: Path) -> None:
	"""The same question answered the other way: the byte after the escape is
	content whatever it is, itself included. `a\\,b,` ends at the fifth byte,
	so the member is five bytes and `after` starts there."""
	blob    = packed_text(ESCAPED)
	message = b"a\\,b,\x00\xff"

	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)
	assert c_widths(tmp_path, blob, message) == ["5", "2"]
	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)
	assert c_answers(tmp_path, blob, message) == ["refused", "255"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_table_the_image_omits_reads_as_empty(tmp_path: Path) -> None:
	"""An absent section must not leave the caller's stack deciding.

	`situ_walk_open` cleared each table it knew about by name, and the varint
	table was added to the struct and to the section loop without being added
	to that list. udp has no varints, so `varint_rules` binary-searched a
	pointer and a count that were never written -- a segfault under a
	poisoned struct, and correct for as long as the stack happened to be
	zero. It clears the whole struct now, which is the version of this that
	cannot go stale when the next table arrives.

	Every case in this file runs against a poisoned struct, so this is the
	name rather than the only coverage.
	"""
	blob    = image_for(ROOT / "example" / "udp" / "udp.situ")
	message = bytes.fromhex("1f90238200105f2a")

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_truncated_image_is_malformed_rather_than_read(
		tmp_path: Path) -> None:
	"""The image is the least trusted input this component has, so every
	table it names is checked against the whole before anything indexes
	one."""
	blob = image_for(ROOT / "example" / "udp" / "udp.situ")
	(tmp_path / "img").write_bytes(blob[:len(blob) // 2])
	(tmp_path / "drive.c").write_text(
		DRIVER.replace("SHOW", SHOW).replace("ASK", VALUES), encoding="ascii")

	assert COMPILER is not None
	# Not `check=True`, which throws the compiler's own sentence away and
	# leaves a status code to explain a build (invariant 115).
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{WALKER}", str(tmp_path / "drive.c"),
		 str(WALKER / "situ_walk.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"), "00"],
	                     capture_output=True, text=True)

	assert ran.stdout.strip() == "malformed"


_RECURSIVE = """target buffer;
endian big;

struct node [depth = 32, limit = {limit}] {{
	{head}
	{run}
}}
"""

#: The two shapes a struct holds a run of itself in. They share nothing above
#: `struct_extent`: a `while` run asks a predicate after each element, a
#: counted run asks a length program once and multiplies -- except that a
#: recursive element has no width to multiply, which is the case this walker
#: refused outright while `walk.py` walked it.
_RUNS = {
	"while":   ("u8    more;", "node  kids[] while (more != 0);"),
	"counted": ("u8    count;", "node  kids[count];"),
}


def _nest(levels: int) -> bytes:
	"""A message nested `levels` deep, read the same way by both shapes: one
	byte per level, meaning "another follows" as a predicate and "one child"
	as a count."""
	return bytes(1 if i + 1 < levels else 0 for i in range(levels))


def _recursive_image(shape: str, limit: int) -> bytes:
	head, run = _RUNS[shape]
	return _inline_image(_RECURSIVE.format(limit=limit, head=head, run=run))


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("shape", sorted(_RUNS))
@pytest.mark.parametrize("levels", [1, 4, 9, 10])
def test_they_agree_about_a_run_of_a_recursive_struct(
		tmp_path: Path, shape: str, levels: int) -> None:
	"""Both run shapes, on both sides of the ceiling, in both walkers.

	This found two faults that neither walker's own tests could, because
	each was internally consistent:

	The counted run of variable-length elements was `SITU_WALK_UNSUPPORTED`
	here and walked in `walk.py` since 26.267 -- so `node children[count]`,
	which is the shape a recursive type most naturally takes, was measurable
	in one reader and not the other.

	And `depth` counted hops through `situ_walk.c` rather than levels of
	struct: `struct_extent` spent one reaching a member and `size_bits_deep`
	spent another descending into it, so a struct level cost two and
	`[limit = 8]` bought four here against `walk.py`'s eight. Both walkers
	refused deep messages and both refused them by name; they simply refused
	different messages, which is invisible to either one alone.

	`levels` straddles the ceiling deliberately: nine structs is the last
	message a `[limit = 8]` schema admits -- the bound counts edges and the
	root is zero -- and ten is the first it does not, so a walker that stops
	early and one that stops late are both caught, and a walker that never
	stops is caught by the second of them.
	"""
	blob    = _recursive_image(shape, 8)
	message = _nest(levels)

	assert c_widths(tmp_path, blob, message) == python_widths(blob, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("shape", sorted(_RUNS))
def test_the_schema_moves_both_walkers_together(
		tmp_path: Path, shape: str) -> None:
	"""Agreement is worth nothing if both agree on a constant.

	The test above compares two readers at one limit, and would pass for two
	walkers that had both ignored the schema and used the same built-in
	number -- which is exactly the state 0054 was written to leave. So this
	one changes the schema and asserts that what both walkers admit changes
	with it: a four-level message is refused under `[limit = 2]` and read
	under `[limit = 8]`, in C and in Python.
	"""
	deep = _nest(4)

	shallow_c = c_widths(tmp_path, _recursive_image(shape, 2), deep)
	deeper_c  = c_widths(tmp_path, _recursive_image(shape, 8), deep)

	assert shallow_c == python_widths(_recursive_image(shape, 2), deep)
	assert deeper_c  == python_widths(_recursive_image(shape, 8), deep)
	assert "refused" in shallow_c, shallow_c
	assert "refused" not in deeper_c, deeper_c


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
@pytest.mark.parametrize("shape", sorted(_RUNS))
@pytest.mark.parametrize(("levels", "expected"),
                         [(3, "0"), (9, "0"), (10, "cannot-say")])
def test_a_message_past_the_ceiling_is_unanswerable_in_both(
		tmp_path: Path, shape: str, levels: int, expected: str) -> None:
	"""`validate` has two channels, and the ceiling belongs to the other one.

	Past `[limit]` a message is *well formed* and refused anyway -- 26.284's
	reasoning, and why the four generated backends spell it `SITU_ERR_DEPTH`
	rather than a constraint error. The walker has no such verdict and should
	not grow one: what it has is the second channel, "this build cannot say",
	which carries exactly that meaning.

	Python answered **0** here, off a walk that had stopped: `_validate`
	breaks on `Unplaceable`, correctly, for a member no backend emits an
	offset for -- and the ceiling was arriving wearing that exception. C
	answered `cannot-say` for the same bytes throughout. Two validators, one
	image, opposite answers, and the differential that exists to catch
	exactly this had no recursive schema in its corpus.

	The expected values are pinned rather than only compared, because two
	walkers agreeing on OK for a message neither followed is agreement.

	Nine and ten rather than eight and nine: `[limit = 8]` counts EDGES, so
	nine structs is the deepest message inside it. The pair moved when the
	walkers were held to the generated code for the first time and turned
	out to have been refusing one level early -- for every recursive schema,
	not only a mutual one (26.288). This test was pinning the walkers'
	answer to each other, which they had agreed on and which was wrong.
	"""
	blob    = _recursive_image(shape, 8)
	message = _nest(levels)

	assert c_verdict(tmp_path, blob, message) == python_verdict(blob, message)
	assert c_verdict(tmp_path, blob, message) == expected


OVERSHOT = """target buffer;
endian big;

struct inner {
	u8  a[]  until ";";
}

struct outer {
	u8     key[]  until ",";
	u8     one;
	u8     two;
	inner  held;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_offset_stops_at_a_short_frame(
		tmp_path: Path) -> None:
	"""An offset chain saturates at the frame, as `situ_advance_u32` does.

	`key` has no delimiter in these bytes, so it reaches the end and the two
	scalars after it are chains that overshoot: 29 + 1 + 1 against a frame of
	29. Both walkers summed without a cap, agreed with each other perfectly,
	and disagreed with all four backends -- they produced 30 and 31, refused
	the reads behind them, and `held` came back `ok=0`.

	The generated C for this schema, measured, answers `0 29 29 29` and takes
	a zero-length sub-view for `held` (`ok=1 extent=0`), because every term
	goes through `situ_advance_u32(offset, term, view.limit)` and
	`situ_in_bounds(view, limit, 0)` is true. 26.27 argues the rule: a term
	is a length the message chose, and an offset past a short frame is a
	pointer nothing downstream can check. Calling such a message malformed
	is `validate`'s job, not a measurement's.

	So the numbers are pinned to the BACKENDS' answer rather than only
	compared between the walkers -- which is the whole finding, since the two
	walkers being one witness is what let this stand.
	"""
	blob    = packed_text(OVERSHOT)
	message = bytes.fromhex("b19e18d650128f00a574a0d06dce57b3c7718ecaf121b"
	                        "b6023bc31dbcc")
	outer   = shape_named_text(OVERSHOT, "outer")

	assert len(message) == 29
	assert b"," not in message

	assert c_offsets(tmp_path, blob, message, outer) == python_offsets(
		blob, message, outer)
	assert c_offsets(tmp_path, blob, message, outer) == ["0", "29", "29", "29"]


UNREACHED = """target buffer;
endian big;

struct short_arm { u8  a; }
struct long_arm  { u8  a; u8  b; u8  c; }

struct dispatched {
	u8  key[]  until ",";
	u8  kind;
	variant body switch (kind) {
		case 'x': short_arm  as_short;
		default:  long_arm   as_long;
	}
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_unreached_discriminant_reads_as_zero(
		tmp_path: Path) -> None:
	"""A discriminant the frame does not reach is 0, and the default arm
	answers -- which is what the four backends do, not a choice made here.

	Their generated getter carries the guard and the reason: "Its offset is a
	sum of lengths the message chose, and the frame does not reach it.
	`validate` reports such a message." Both walkers refused instead, and the
	refusal did not stay local: `struct_extent` sums `size_bits`, so ONE
	unreachable discriminant made the whole enclosing struct unmeasurable.

	Found in json, where `member.held` answered `ok=0` against all four
	backends' `ok=1 extent=0` -- and `variant_bits` already said two lines
	below the read that a discriminant naming no arm is `validate`'s business
	and not an extent's. The unreachable case is the same sentence one step
	earlier, and only the reachable half had been written.

	`key` eats the frame here, so `kind` sits at the limit and reads as 0,
	which selects `long_arm` -- three bytes, none of them present. The extent
	is what a measurement says and `validate` is what complains.

	The numbers are the BACKENDS' and not the two walkers'. Generated C for
	this schema and these bytes, measured:

	    key span       17
	    kind offset    17
	    kind value      0      <- the rule, stated by the code that has it
	    as_long offset 17
	    as_long view   refused <- the VIEW, which is a different question

	A measurement of 3 and a refused sub-view are both right and are not the
	same answer: `situ_view_sub` will not hand out bytes that are not there,
	while the extent says how far the member would reach. The walkers are
	held to the first.
	"""
	blob    = packed_text(UNREACHED)
	message = b"no delimiter here"
	shape   = shape_named_text(UNREACHED, "dispatched")

	assert b"," not in message

	assert c_widths(tmp_path, blob, message, shape) == python_widths(
		blob, message, shape)
	assert c_widths(tmp_path, blob, message, shape) == ["17", "1", "3"]


INDEXED = """target buffer;
endian big;

struct cell {
	u8  payload[2];
}

struct paged {
	u8   kind  [must_eq = 13];
	u16  count;

	indexed(offset_type = u16, count = count, base = kind) {
		cell  cells[];
	}
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_they_agree_that_an_index_table_has_to_fit_the_frame(
		tmp_path: Path) -> None:
	"""An `indexed` region's offset table is `count` entries of its entry
	width, and `validate` is where a count the frame cannot hold is called
	malformed.

	Neither walker could ask this. The image has written an INDEXES section
	since indexed regions arrived, nothing in `walker/` loaded it, AND the
	packer wrote `none` into the section's `count_code` -- so loading it
	alone would not have been enough either. The packer therefore marked any
	struct holding an indexed region unvalidatable, and both walkers said
	`cannot-say` where four backends answer.

	Found when a differential alphabet change drew a sqlite page declaring
	2644 cells in a 46-byte frame: C said BOUNDS, the walk said clean, and
	it had been saying so for as long as the construct existed -- invisible
	because no draw had reached it.

	The boundary is pinned rather than only the two extremes, because a
	check that fires for every count and one that fires for the right ones
	both refuse 2644. Here the table starts at byte 3 of a 16-byte frame and
	each entry is 2 bytes, so 6 fit and 7 do not. `example/sqlite` gives the
	same arithmetic against the generated C: a 46-byte page with the table
	at 8 and 2-byte entries validates at 19 cells and refuses at 20.
	"""
	blob, image = _packed_named(INDEXED)
	shape = [image.struct_name(i)
	         for i in range(len(image.structs))].index("paged")

	def page(count: int) -> bytes:
		return bytes([13]) + count.to_bytes(2, "big") + bytes(13)

	# "0" is OK and "1" is BOUNDS, which is `situ_err_t`'s own numbering and
	# the vocabulary every other case in this file is written in.
	for count, expected in ((0, "0"), (6, "0"), (7, "1"), (2644, "1")):
		message = page(count)
		assert len(message) == 16
		assert c_verdict(tmp_path, blob, message, shape) \
			== python_verdict(blob, message, shape), f"count={count}"
		assert c_verdict(tmp_path, blob, message, shape) == expected, \
			f"count={count}"
