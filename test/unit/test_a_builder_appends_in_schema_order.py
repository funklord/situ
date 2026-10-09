"""The write half of rung 2: what a builder writes, and what has none.

hull adopted situ for a format it also has to WRITE, and found that no rung
emitted anything that builds a message although 0032 names rung 2 "build or
resize a message whose extent is not fixed" (suggestion/hull.md, 11).
`--owned` encodes, but only a struct whose every member has a fixed-size C
field -- so what was missing was exactly the variable-extent case.

Four things are held here, and they fail on different axes:

  - a size field is COMPUTED from the run it measures, never asked for, so
    a message cannot disagree with its own schema;
  - a layout that cannot be written forward has no builder, and is named;
  - every struct is in one of those two sets, so nothing is silently
    absent -- the partition rather than either cell, since the interesting
    cell is the one a later increment will empty;
  - what is emitted compiles, and what it writes reads back through the
    view accessors.

The compile sweep is not decoration. It caught four faults in the first
draft: `<reserved0>` reaching a parameter list verbatim in four schemas,
and a register struct whose view function does not exist because a
register is a bus transaction rather than bytes (15.1).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from every_schema import SCHEMAS, load_schema	# noqa: E402
from situc import traverse			# noqa: E402
from situc.codegen.c import build		# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.resolve import resolve		# noqa: E402

RUNTIME  = ROOT / "runtime" / "c"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
WARNINGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")


def _parts(path: Path):				# type: ignore[no-untyped-def]
	parsed   = load_schema(path)
	resolved = resolve(parsed, solve(parsed))
	return parsed, resolved


def _only(resolved, name: str):			# type: ignore[no-untyped-def]
	struct = resolved.structs[name]
	return build.plan(struct)


def test_a_size_field_is_computed_rather_than_asked_for() -> None:
	"""mqtt_string is the length-prefixed shape, which is hull's atom with a
	binary length instead of a decimal one.

	The assertion is the relationship rather than the parameter list: the
	member named by `sized_by` must be a SIZE part and must not appear in
	the signature, because a caller able to pass both a length and a run
	is a caller able to make them disagree.
	"""
	parsed, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	parts, why = _only(resolved, "mqtt_string")
	assert why is None, why

	roles = {part.placement.name: part.role for part in parts}
	assert roles == {"length": build.SIZE, "value": build.SPAN}, roles

	text = build.generate(parsed, resolved, "mqtt")["mqtt_build.h"]
	body = text.split("situ_mqtt_string_build(")[1].split("\n}")[0]
	assert "uint32_t value_len" in body
	assert "length" not in body.split("{")[0], \
		"the length is a parameter, so a caller can contradict the schema"
	assert "(uint16_t)value_len" in body and "situ_put_be16" in body


def test_reserved_bytes_take_no_parameter() -> None:
	"""Reserved bytes are a constraint rather than a value (8.8), so there
	is nothing for a caller to pass -- and zero is not a choice either: the
	content policy is `must_be_zero` or nothing, so zero is required where
	it is stated and refused nowhere.

	The first draft asked for them, under the compiler's own label for an
	unnamed field, and four schemas would not compile.
	"""
	parsed, resolved = _parts(ROOT / "example" / "icmp" / "icmp.situ")
	parts, why = _only(resolved, "icmp_frag")
	assert why is None, why
	assert [part.role for part in parts] == [build.ZERO, build.SCALAR]

	text = build.generate(parsed, resolved, "icmp")["icmp_build.h"]
	head = text.split("situ_icmp_frag_build(")[1].split("{")[0]
	assert "<" not in head, f"a synthesised label reached the signature: {head}"
	assert f"memset({build.OUT} + {build.AT}, 0, 2u)" in text


def test_a_member_name_a_parameter_cannot_carry_is_moved() -> None:
	"""0025's argument met by a third caller: the schema keeps its name and
	the emitter moves.

	`test/schema/edges.situ` has a member called `short`, which is legal in
	a schema and cannot be a C parameter. Mangling costs a caller nothing
	here and that is specific to C: there are no named arguments, so a
	parameter name is documentation rather than call syntax. Refusing the
	struct would have cost them the builder.

	The assertion is that the member is still itself and the parameter is
	not, rather than that any particular spelling was chosen -- which is
	`bare_name`'s to decide and is already tested where it lives.
	"""
	_, resolved = _parts(ROOT / "test" / "schema" / "edges.situ")
	parts, why  = _only(resolved, "keywords")
	assert why is None, why

	mine = [part for part in parts if part.placement.name == "short"]
	assert mine, "`short` is not a member of `keywords` any more"
	assert mine[0].local != "short", "`short` reached the signature unchanged"


def test_the_writer_s_own_locals_are_out_of_the_schema_s_namespace() -> None:
	"""`std/image.situ` has a member called `cap`.

	Against an earlier draft whose capacity parameter was `cap`, that was
	two errors from one collision -- a redefinition and then an unused
	parameter -- and the schema is a legal one.

	**Two mechanisms each save that case on their own**, which is worth
	stating because it means neither sabotage goes red alone. Measured, in
	four cells: with the prefix and the uniquifier, green; with the prefix
	removed, green, the uniquifier mangling `cap` to `cap_`; with the
	uniquifier disabled, green, the prefix keeping them apart; with both
	gone, `image` does not compile. So this test is about the prefix as a
	QUALITY property -- a member keeps the spelling the schema gave it --
	and the test below gives the uniquifier a population of its own, since
	this corpus leaves it empty.
	"""
	assert all(name.startswith("situ_") for name in build.FIXTURES)

	_, resolved = _parts(ROOT / "std" / "image.situ")
	parts, why  = _only(resolved, "image_delimiter")
	assert why is None, why
	assert "cap" in [part.local for part in parts]


@pytest.mark.parametrize("schema,name,wanted", [
	# A checksum INSIDE the region it sums: the writer would have to go
	# back, which is what append-only means it cannot do.
	("ipv4/ipv4.situ", "ipv4_header", "which follows it"),
	# An index table holds the offsets of elements that come after it.
	("sqlite/sqlite.situ", "btree_leaf_page", "index table"),
	# A located member lands where an expression says, not where the
	# writer had reached.
	("bmp/bmp.situ", "bitmap_file", "located by an expression"),
])
def test_a_layout_that_cannot_be_written_forward_is_refused(
		schema: str, name: str, wanted: str) -> None:
	_, resolved = _parts(ROOT / "example" / schema)
	parts, why  = _only(resolved, name)
	assert not parts
	assert why is not None and wanted in why, why


def test_a_trailing_tag_does_not_make_a_layout_unwritable() -> None:
	"""The discriminating case for the positional half, and the reason
	`covered_members` is consulted rather than `tag_covers` alone.

	PNG's CRC covers the chunk's type and data and comes AFTER both, so
	nothing about appending refuses it -- it is refused for a different
	reason, that this writer computes no checksum. A predicate that
	refused every tag would pass the three cases above and say nothing.
	"""
	_, resolved = _parts(ROOT / "example" / "png" / "png.situ")
	chunk = resolved.structs["chunk"]
	assert traverse.append_only_refusals(chunk) == []
	_, why = build.plan(chunk)
	assert why is not None and "does not compute" in why, why


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.stem)
def test_every_struct_either_builds_or_is_named(path: Path) -> None:
	"""The partition, asserted instead of either cell.

	A caller who asked for a builder and found their struct missing would
	conclude the generator was broken, which is what `--owned` already
	says about its own refusals. And the cell that matters is the refused
	one: it is what a later increment empties, and an entry arriving in it
	unnamed is how that increment would go unnoticed.
	"""
	_, resolved = _parts(path)
	built   = {struct.name for struct in build.buildable(resolved)}
	refused = dict(build.refusals(resolved))

	assert not (built & set(refused)), built & set(refused)
	assert built | set(refused) == set(resolved.structs)
	for name, why in refused.items():
		assert why and why[0] in "`ihacr", f"{name}: {why}"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.stem)
def test_the_build_header_compiles(path: Path, tmp_path: Path) -> None:
	parsed, resolved = _parts(path)
	files = dict(build.generate(parsed, resolved, path.stem))
	if not files:
		pytest.skip("no struct in this schema can be built forward")

	files.update(generate_c(parsed, resolved, path.stem).files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	probe = tmp_path / "tu.c"
	probe.write_text(f'#include "{path.stem}_build.h"\n'
	                 "int main(void) { return 0; }\n", encoding="ascii")
	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 "-c", str(probe), "-o", str(tmp_path / "tu.o")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_what_a_builder_writes_reads_back_through_the_view(
		tmp_path: Path) -> None:
	"""The round trip, and three refusals beside it.

	The over-long run is the discriminating one: a length no `u16` can
	hold must come back CONSTRAINT rather than BOUNDS, because BOUNDS is
	what the capacity check alone would say. With the limit check removed
	the cap check catches the same call and reports the wrong thing.
	"""
	parsed, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	files = dict(build.generate(parsed, resolved, "mqtt"))
	files.update(generate_c(parsed, resolved, "mqtt").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "mqtt_build.h"

int main(void)
{
	uint8_t     buf[32];
	uint32_t    wrote = 0, kept = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	const uint8_t hello[5] = { 'h', 'e', 'l', 'l', 'o' };

	if (situ_mqtt_string_build(buf, sizeof buf, hello, 5, &wrote) != SITU_OK)
		return 2;
	situ_msg_init(&msg, buf, wrote);
	if (situ_mqtt_string_view(&msg, 0, wrote, &view) != SITU_OK) return 3;
	printf("%u %u %.5s\\n", wrote,
	       (unsigned)situ_mqtt_string_length_get(view), buf + 2);

	/* zero length is a legal string, and the run may then be NULL */
	err = situ_mqtt_string_build(buf, sizeof buf, NULL, 0, &kept);
	printf("%d %u\\n", (int)err, kept);

	/* a capacity one short of what the members need */
	kept = 0;
	err = situ_mqtt_string_build(buf, wrote - 1, hello, 5, &kept);
	printf("%d %u\\n", (int)err, kept);

	/* a length no u16 can say, with a capacity too small either way */
	err = situ_mqtt_string_build(buf, 16, hello, 70000, &kept);
	printf("%d\\n", (int)err);
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "mqtt.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")

	assert lines[0] == "7 5 hello", lines[0]
	assert lines[1] == "0 2", f"an empty string is legal: {lines[1]}"
	assert lines[2] == "1 0", \
		f"a short capacity is BOUNDS, and leaves `*wrote` alone: {lines[2]}"
	assert lines[3] == "2", \
		f"a run longer than its size field is CONSTRAINT, not BOUNDS: {lines[3]}"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_the_schema_refuses_what_the_builder_wrote(tmp_path: Path) -> None:
	"""The control for the tail, and the reason the tail is there at all.

	A builder that bounds-checks every member can still lay down a message
	the schema refuses, because a scalar's legal RANGE is not its width:
	arp's `operation` is an enum whose unknown values are an error, and 99
	is one. The build must come back CONSTRAINT.

	It is the view's own `_validate` that says so rather than a second
	opinion written here, for the reason `owned.py` states: two checks of
	one schema is how they come to disagree. With the tail removed this
	returns SITU_OK and the bytes are still wrong.
	"""
	parsed, resolved = _parts(ROOT / "example" / "arp" / "arp.situ")
	files = dict(build.generate(parsed, resolved, "arp"))
	files.update(generate_c(parsed, resolved, "arp").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "arp_build.h"

int main(void)
{
	uint8_t  buf[64];
	uint32_t wrote = 0;
	const uint8_t six[6] = { 1, 2, 3, 4, 5, 6 };
	const uint8_t four[4] = { 10, 0, 0, 1 };

	printf("%d\\n", (int)situ_arp_packet_build(buf, sizeof buf, 1, 0x0800,
	                                           6, 4, 1, six, four, six,
	                                           four, &wrote));
	printf("%u\\n", wrote);
	/* 99 is not an operation ARP names */
	printf("%d\\n", (int)situ_arp_packet_build(buf, sizeof buf, 1, 0x0800,
	                                           6, 4, 99, six, four, six,
	                                           four, &wrote));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "arp.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split()
	assert lines[0] == "0", f"a well-formed packet did not build: {lines}"
	assert lines[1] == "28", f"an arp packet is 28 bytes: {lines}"
	assert lines[2] == "2", \
		f"an operation the enum does not name must be refused: {lines}"


def test_two_members_that_would_reach_one_parameter_are_kept_apart() -> None:
	"""The uniquifier's own population, which the corpus does not hold.

	A span emits a `_len` beside itself, so a schema carrying both `value`
	and `value_len` reaches one parameter twice -- and a member spelled like
	one of the writer's own locals reaches it once too. Neither appears in
	any schema here, so this is constructed rather than measured: the
	alternative is a guard whose only evidence is that nothing has needed
	it, which is the same evidence a broken one leaves.

	Declaration order decides, so the member that came first keeps its
	spelling and the span moves.
	"""
	from situc.diagnostics import Source
	from situc.parser import parse as parse_text

	parsed = parse_text(Source("inline", """
target buffer;
endian big;

struct collide {
	u16 value_len;
	u16 situ_at;
	u16 value_size;
	u8  value[value_size];
}
"""))
	resolved = resolve(parsed, solve(parsed))
	parts, why = build.plan(resolved.structs["collide"])
	assert why is None, why

	locals_ = [part.local for part in parts]
	assert len(set(locals_)) == len(locals_), locals_
	assert not set(locals_) & build.FIXTURES, locals_

	spelt = {part.placement.name: part.local for part in parts}
	assert spelt["value_len"] == "value_len", "the first member keeps its name"
	assert spelt["value"] != "value", "the span's `_len` would collide"
	assert spelt["situ_at"] != "situ_at", "that is the writer's cursor"
	assert f"{spelt['value']}_len" not in locals_


def _inline(body: str):				# type: ignore[no-untyped-def]
	"""A schema from a string, for a shape no corpus schema holds."""
	from situc.diagnostics import Source
	from situc.parser import parse as parse_text

	parsed = parse_text(Source("inline", "target buffer;\nendian big;\n\n"
	                           + body))
	return parsed, resolve(parsed, solve(parsed))


#: Rivest's canonical atom, which is what hull adopted situ to write:
#: a minimal decimal length, a colon, and that many bytes. Mirrored here
#: rather than read from hull's tree, because a test that reads another
#: project's working copy measures whatever that session left it as.
ATOM = """struct atom {
\tdecimal u32 length until ":" max 10 [minimal];
\tu8 bytes[length];
}
"""


def test_a_minimal_run_of_digits_is_written_with_its_terminator() -> None:
	"""The radix path, and what it is built on.

	`situ_format_uint` already writes digits -- it is `situ_parse_uint`
	backwards -- at a fixed width, so leading zeros are mandatory and one
	value is one byte sequence. `[minimal]` is the case it cannot do, and
	what is missing for it is the WIDTH rather than the conversion: three
	lines counting digits, then the same function.

	So this asserts that the runtime's formatter is what writes the digits.
	A second implementation here is the fault `owned.py` paid for in BCD,
	where an encode and a decode were self-consistent and wrong together.
	"""
	parsed, resolved = _inline(ATOM)
	parts, why = _only(resolved, "atom")
	assert why is None, why

	roles = {part.placement.name: part.role for part in parts}
	assert roles == {"length": build.SIZE, "bytes": build.SPAN}, roles
	length = [part for part in parts if part.placement.name == "length"][0]
	assert length.radix == 10 and length.minimal
	assert length.delimiter == b":" and length.digits == 10

	text = build.generate(parsed, resolved, "canonical")["canonical_build.h"]
	assert "situ_format_uint" in text, "the digits are written here instead"
	assert "0x3Au" in text, "the colon the schema states is not written"


@pytest.mark.parametrize("member,wanted", [
	# Several bytes may end it, so which one to write is not stated. The
	# case with NO delimiter is absent because the parser refuses it before
	# this writer ever sees it.
	('decimal u32 n until ":" | " " max 4 [minimal]', "any of 2 delimiters"),
	# `5` is a decimal digit, so the digits cannot stop at it: a minimal run
	# ends at the first byte that is not one.
	('decimal u32 n until "5" max 4 [minimal]', "which is a digit"),
	# Neither a width nor `[minimal]`, so how many digits to write is not
	# stated anywhere.
	('decimal u32 n until ":"', "neither a width"),
	# The runtime formats unsigned values; a sign is a second thing to
	# write and is not written here.
	('decimal i32 n until ":" max 4 [minimal]', "signed number"),
])
def test_digits_the_writer_cannot_place_are_refused(
		member: str, wanted: str) -> None:
	"""Each of these is a schema situ accepts and this writer does not.

	They are constructed rather than drawn from the corpus, which holds no
	instance of any of them -- so without these the preconditions would be
	four conditions whose only evidence is that nothing has tripped them.
	"""
	_, resolved = _inline("struct one {\n\t%s;\n\tu8 rest[4];\n}\n"
	                      % member)
	_, why = _only(resolved, "one")
	assert why is not None and wanted in why, why


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_canonical_atom_is_built_and_reads_back(tmp_path: Path) -> None:
	"""The adopter's own shape, end to end.

	`0:` is the case the digit count gets wrong if it starts from zero
	rather than one, and the empty run is legal, so it is here beside
	`4:root`. `10:` is the case a one-digit assumption gets wrong.
	"""
	parsed, resolved = _inline(ATOM)
	files = dict(build.generate(parsed, resolved, "canonical"))
	files.update(generate_c(parsed, resolved, "canonical").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include <string.h>
#include "canonical_build.h"

static void show(const uint8_t *in, uint32_t len)
{
	uint8_t     buf[64];
	uint32_t    wrote = 0;
	situ_err_t  err = situ_atom_build(buf, sizeof buf, in, len, &wrote);

	printf("%d %u ", (int)err, wrote);
	if (err == SITU_OK) fwrite(buf, 1, wrote, stdout);
	printf("\\n");
}

int main(void)
{
	uint8_t     big[20];
	uint8_t     small[8];
	uint32_t    wrote = 0;
	situ_msg_t  msg;
	situ_view_t view;

	show((const uint8_t *)"root", 4);
	show((const uint8_t *)"", 0);
	show((const uint8_t *)"0123456789", 10);
	memset(big, 'x', sizeof big);
	show(big, sizeof big);

	/* room for neither the digits, the colon, nor the run */
	printf("%d\\n", (int)situ_atom_build(small, 4,
	                                     (const uint8_t *)"root", 4, &wrote));

	/* and the length reads back through the view */
	if (situ_atom_build(big, sizeof big, (const uint8_t *)"root", 4,
	                    &wrote) != SITU_OK) return 2;
	situ_msg_init(&msg, big, wrote);
	if (situ_atom_view(&msg, 0, wrote, &view) != SITU_OK) return 3;
	{
		uint32_t back = 0;

		if (situ_atom_length_get(view, &back) != SITU_OK) return 4;
		printf("%u\\n", back);
	}
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "canonical.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, f"exited {ran.returncode}: {ran.stderr}"
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 6 4:root", lines[0]
	assert lines[1] == "0 2 0:", f"an empty atom is `0:`: {lines[1]}"
	assert lines[2] == "0 13 10:0123456789", \
		f"ten bytes need two digits: {lines[2]}"
	assert lines[3] == "0 23 20:" + "x" * 20, lines[3]
	assert lines[4] == "1", f"a capacity of four cannot hold it: {lines[4]}"
	assert lines[5] == "4", f"the length does not read back: {lines[5]}"


#: A plain delimited run followed by a counted one. Deliberately carrying no
#: enum and no constraint, because that is what makes it the only fixture
#: that can answer for the scan: see the test below.
ENDED = """struct ended {
\tu8 name[] until " ";
\tu8 rest[4];
}
"""


def test_a_run_is_ended_by_writing_its_delimiter() -> None:
	"""The shape 23 of the corpus's 31 refused delimited members have.

	Measured when this was written, over the 45 schemas: a single consumed
	delimiter, no escape, no quote, no trim. The tail shapes are refused by
	name and tested below.
	"""
	parsed, resolved = _inline(ENDED)
	parts, why = _only(resolved, "ended")
	assert why is None, why
	assert [part.role for part in parts] == [build.ENDED, build.RUN]

	text = build.generate(parsed, resolved, "ended")["ended_build.h"]
	body = text.split("situ_ended_build(")[1]
	assert "uint32_t name_len" in body
	assert f"{build.OUT}[{build.AT}] = 0x20u" in body, \
		"the delimiter the schema states is not written"


def test_a_delimited_member_is_a_run_and_not_a_one_byte_scalar() -> None:
	"""`u8 name[] until " "` carries a u8 scalar and no count.

	That is exactly the shape the single-scalar branch takes, and the first
	draft let it: four more structs reported as buildable with no delimited
	part among them. The count said the work was done and the thing it
	counted had gone somewhere else -- which is why the measurement that
	found it was per ROLE rather than per struct.

	So this asserts the role, not that the struct builds.
	"""
	_, resolved = _inline(ENDED)
	parts, _ = _only(resolved, "ended")
	name = [part for part in parts if part.placement.name == "name"][0]
	assert name.role == build.ENDED, \
		f"a delimited run planned as {name.role}, which writes one byte"


@pytest.mark.parametrize("member,wanted", [
	# Several bytes may end it, so which to write is not stated. http's
	# `header_field.value` is the corpus instance.
	('u8 x[] until " " | "\\t"', "any of 2 delimiters"),
	# `\r\n` is two bytes; http's and smtp's lines all end this way, which
	# is why they gain nothing from this increment.
	('u8 x[] until "\\r\\n"', "multi-byte delimiter"),
	# Escaping would write bytes other than the ones the caller gave, and
	# the reader's unescape has no writer to be tested against.
	('u8 x[] until "\\"" [escape = "\\\\"]', "escapes its delimiter"),
	# Trimming is the same: the bytes it holds are not the bytes given.
	('u8 x[] until " " [trim = " "]', "is trimmed"),
])
def test_a_delimiter_the_writer_cannot_place_is_refused(
		member: str, wanted: str) -> None:
	_, resolved = _inline("struct one {\n\t%s;\n\tu8 rest[2];\n}\n"
	                      % member)
	_, why = _only(resolved, "one")
	assert why is not None and wanted in why, why


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_run_carrying_its_own_delimiter_is_refused(tmp_path: Path) -> None:
	"""The control for the scan, and it took two attempts to aim.

	The first fixture was `edges.spoken`, whose runs are typed by byte-run
	enums -- and with the scan deleted that case STILL came back
	CONSTRAINT, because `greeting` closes with `default = error` and the
	truncated value is not a greeting. The scan was being credited with a
	refusal the enum was making: a control that cannot be reached, since
	something upstream answers first.

	This fixture has no enum and no constraint, so nothing but the scan can
	refuse it -- and what the absence costs is visible rather than
	theoretical. `name` = `a b` writes `a b` + `wxyz`; read back, `name`
	stops at the first space and `rest` starts two bytes early, so the
	message is valid, different from the one asked for, and SITU_OK.
	"""
	parsed, resolved = _inline(ENDED)
	files = dict(build.generate(parsed, resolved, "ended"))
	files.update(generate_c(parsed, resolved, "ended").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "ended_build.h"

int main(void)
{
	uint8_t  buf[32];
	uint32_t wrote = 0;

	/* a run with no delimiter in it. Two statements, because reading
	 * `wrote` in the same call that sets it is unsequenced. */
	{
		situ_err_t err = situ_ended_build(buf, sizeof buf,
		                                  (const uint8_t *)"ab", 2,
		                                  (const uint8_t *)"wxyz", &wrote);

		printf("%d %u\\n", (int)err, wrote);
	}
	/* and one carrying the space that ends it */
	printf("%d\\n", (int)situ_ended_build(buf, sizeof buf,
	                                     (const uint8_t *)"a b", 3,
	                                     (const uint8_t *)"wxyz", &wrote));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "ended.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 7", f"`ab wxyz` is seven bytes: {lines[0]}"
	assert lines[1] == "2", \
		f"a run containing its own delimiter must be refused: {lines[1]}"


@pytest.mark.parametrize("schema,struct,wanted", [
	# `before`, and it lists three. The count must not be what it is told
	# about, because it writes no terminator at all.
	("sexpr/sexpr.situ", "symbol", "is a `before` run"),
	# `until`, and it lists two. Here the count IS the reason: the run ends
	# at one of them and which to write is not stated.
	("http/http.situ", "header_field", "any of 2 delimiters"),
])
def test_a_before_run_is_not_told_about_its_delimiter_count(
		schema: str, struct: str, wanted: str) -> None:
	"""Two corpus instances that separate two checks, in that order.

	A `before` run writes no terminator -- the delimiter belongs to
	whatever follows -- so the number it lists does not bear on it. Asked
	count-first, `sexpr.symbol.name` was told *which byte to write after it
	is not stated* when no byte is its to write: a refusal that was right
	with a reason that was not.

	Reported by hull, who read the function rather than its output; their
	own `symbol` lists eight delimiters. Both refusals stand -- what a
	`before` run cannot guarantee is that the bytes written AFTER it begin
	with a delimiter, and that belongs to whatever composes it, which is
	the run-of-structs case still missing.
	"""
	_, resolved = _parts(ROOT / "example" / schema)
	_, why = _only(resolved, struct)
	assert why is not None and wanted in why, why
