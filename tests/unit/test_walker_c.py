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
from walker.walk import Refused, Unplaceable, acquire, read_scalar, size_bits

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


def image_for(path: Path) -> bytes:
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	return pack(schema, resolved)[0]


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
	blob    = image_for(ROOT / "examples" / "udp" / "udp.situ")
	message = bytes.fromhex("1f90238200105f2a")

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_variable_member_is_refused_rather_than_guessed(
		tmp_path: Path) -> None:
	"""udp's payload has no constant extent, and this build says so. A
	number here would be a wrong length that reads like a right one."""
	blob = image_for(ROOT / "examples" / "udp" / "udp.situ")

	answers = c_answers(tmp_path, blob, bytes.fromhex("1f90238200105f2a"))

	assert answers[-1] == "refused"
	assert all(one != "refused" for one in answers[:-1])


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


#: `beats` from `tests/schemas/edges.situ`, whose header says the walk is
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
	blob    = image_for(ROOT / "examples" / "udp" / "udp.situ")
	message = bytes.fromhex("1f90238200105f2a")

	assert c_answers(tmp_path, blob, message) == python_answers(blob, message)


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_truncated_image_is_malformed_rather_than_read(
		tmp_path: Path) -> None:
	"""The image is the least trusted input this component has, so every
	table it names is checked against the whole before anything indexes
	one."""
	blob = image_for(ROOT / "examples" / "udp" / "udp.situ")
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
