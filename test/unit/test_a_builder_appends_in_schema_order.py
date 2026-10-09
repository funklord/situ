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
from situc.layout import Arm, solve		# noqa: E402
from situc.resolve import resolve		# noqa: E402

RUNTIME  = ROOT / "runtime" / "c"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
WARNINGS = ("-std=c11", "-O1", "-Wall", "-Wextra", "-Werror")


def _parts(path: Path):				# type: ignore[no-untyped-def]
	parsed   = load_schema(path)
	resolved = resolve(parsed, solve(parsed))
	return parsed, resolved


def _header(parsed, resolved, stem: str) -> str:	# type: ignore[no-untyped-def]
	"""The ordinary header, which is what says who has a `_required`.

	`situc build` passes it because it has just emitted it; a test that
	did not would refuse every nested member and exercise none of them.
	"""
	return generate_c(parsed, resolved, stem).files().get(f"{stem}.h", "")


def _only(resolved, name: str, header: str = "",	# type: ignore[no-untyped-def]
		arm: Arm | None = None):
	struct = resolved.structs[name]
	return build.plan(struct, header, "situ", arm, resolved.structs)


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
	parsed, resolved = _parts(path)
	header  = _header(parsed, resolved, path.stem)
	built   = {struct.name for struct in build.buildable(resolved, header)}
	refused = dict(build.refusals(resolved, header))

	# A variant is refused PER ARM, so a refusal names `struct.arm` and a
	# struct can be in both sets at once -- some arms writable and some
	# not. That is the honest shape and this test caught the change to it,
	# which is what it is for.
	named = {key.split(".")[0] for key in refused}
	assert built | named == set(resolved.structs), \
		set(resolved.structs) - (built | named)
	for name, why in refused.items():
		assert why and why[0] in "`ihacr", f"{name}: {why}"

	# And every struct that is neither built nor refused would be silently
	# absent, which is the cell this test exists to keep empty.
	for name, struct in resolved.structs.items():
		if name in built:
			continue
		assert name in named, f"`{name}` is neither built nor named"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.stem)
def test_the_build_header_compiles(path: Path, tmp_path: Path) -> None:
	parsed, resolved = _parts(path)
	files = dict(build.generate(parsed, resolved, path.stem, "situ",
	                            _header(parsed, resolved, path.stem)))
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


def test_a_nested_struct_is_a_field_and_not_a_run_of_them() -> None:
	"""`mqtt_string topic;` is one struct, not a run of them.

	It was refused as *a run of `mqtt_string`, not of bytes* -- a verdict
	that was right with a reason about something else, which is the class
	hull found in the `before` diagnosis. Measured at the time: 8 of the 16
	members refused for "not of bytes" were single nested fields rather
	than runs, and nothing in the message said so.
	"""
	parsed, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	header = _header(parsed, resolved, "mqtt")
	parts, why = _only(resolved, "will_message", header)
	assert why is None, why
	assert [part.role for part in parts] == [build.NESTED, build.NESTED]
	assert [part.inner for part in parts] == ["situ_mqtt_string"] * 2


def test_a_nested_struct_whose_extent_its_bytes_do_not_state_is_refused() -> None:
	"""The guard has a corpus population, which is why it is not empty.

	`edges.edge_varint` has no `_required` -- its extent is not computable
	from its own bytes -- so a caller's length for one could not be
	checked, and three members in that schema are refused by name rather
	than trusted.
	"""
	parsed, resolved = _parts(ROOT / "test" / "schema" / "edges.situ")
	header = _header(parsed, resolved, "edges")
	_, why = _only(resolved, "varint_driver", header)
	assert why is not None and "nested `edge_varint`" in why, why
	assert "its own bytes do not determine" in why

	# And the predicate reads the artifact rather than guessing: the header
	# declares one for `mqtt_string` and none for `edge_varint`.
	assert not build.frames_itself(header, "situ_edge_varint")


def test_a_builder_with_no_header_refuses_rather_than_guesses() -> None:
	"""Without the header there is no way to know who has a `_required`.

	A caller who does not pass it gets less rather than something wrong,
	which is the direction that matters: the alternative is emitting a call
	to a function that may not exist.
	"""
	_, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	_, why = _only(resolved, "will_message")
	assert why is not None and "nested `mqtt_string`" in why, why


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_nested_struct_is_built_by_composing_two_builders(
		tmp_path: Path) -> None:
	"""The compositional case, and the control for asking `_required`.

	Build two `mqtt_string`s with their own builder, then compose them into
	a `will_message`. That is what an adopter does, and it is why a nested
	member takes bytes rather than the inner struct's own parameters:
	`json.value` holds a `value`, so flattening would not terminate.

	The control is the TOO-LONG length, measured rather than assumed. With
	the extent check removed, a length one byte over returns SITU_OK and
	the payload starts a byte late -- `_validate` does not catch it,
	because the shifted read is still a well-formed pair of strings. The
	too-SHORT case it does catch, so only the first discriminates, and the
	byte after the topic is set deliberately so that the shifted read is
	defined rather than whatever the stack held.
	"""
	parsed, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	files = dict(build.generate(parsed, resolved, "mqtt", "situ",
	                            _header(parsed, resolved, "mqtt")))
	files.update(generate_c(parsed, resolved, "mqtt").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "mqtt_build.h"

int main(void)
{
	uint8_t     topic[32], payload[32], whole[64];
	uint32_t    tn = 0, pn = 0, wrote = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	if (situ_mqtt_string_build(topic, sizeof topic,
	                           (const uint8_t *)"a/b", 3, &tn) != SITU_OK)
		return 2;
	if (situ_mqtt_string_build(payload, sizeof payload,
	                           (const uint8_t *)"hi", 2, &pn) != SITU_OK)
		return 3;
	topic[tn] = 0x00;	/* so a shifted read is defined */

	err = situ_will_message_build(whole, sizeof whole, topic, tn,
	                              payload, pn, &wrote);
	printf("%d %u ", (int)err, wrote);
	for (i = 0; i < wrote; i++)
		printf("%02X", whole[i]);
	printf("\\n");
	if (err != SITU_OK) return 4;

	situ_msg_init(&msg, whole, wrote);
	printf("%d\\n", (int)situ_will_message_view(&msg, 0, wrote, &view));

	/* one byte too long: only the inner `_required` sees this */
	printf("%d\\n", (int)situ_will_message_build(whole, sizeof whole,
	                                            topic, tn + 1u,
	                                            payload, pn, &wrote));
	/* one byte too short, which `_validate` would also refuse */
	printf("%d\\n", (int)situ_will_message_build(whole, sizeof whole,
	                                            topic, tn - 1u,
	                                            payload, pn, &wrote));
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
	assert ran.returncode == 0, f"exited {ran.returncode}: {ran.stderr}"
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 9 0003612F6200026869", \
		f"two length-prefixed strings, nine bytes: {lines[0]}"
	assert lines[1] == "0", f"the result does not acquire a view: {lines[1]}"
	assert lines[2] == "2", \
		f"a length one byte over must be refused: {lines[2]}"
	assert lines[3] == "2", f"and one byte under: {lines[3]}"


@pytest.mark.parametrize("schema,struct,member,shape", [
	# A count field naming the number of elements.
	("example/ble/ble.situ", "le_advertising_report", "reports", "num"),
	# The rest of the frame.
	("example/mqtt/mqtt.situ", "unsubscribe_body", "filters", "remaining"),
	# A delimiter.
	("example/sexpr/sexpr.situ", "list", "items", "delimiter"),
	# A literal count.
	("test/schema/edges.situ", "pieces", "two", "fixed"),
])
def test_every_shape_a_struct_run_takes_is_one_walk(
		schema: str, struct: str, member: str, shape: str) -> None:
	"""Four shapes, one mechanism, which is why there is one role.

	A fixed count, a count field, a run that takes the rest of the frame
	and a run ended by a delimiter all need the same question answered --
	are these bytes a whole number of elements -- and the element's own
	`_required` answers it. Deferring them as four design questions was
	wrong, and the thing that was wrong was assuming an incremental API
	where caller-supplied bytes make it a verification loop.
	"""
	parsed, resolved = _parts(ROOT / schema)
	header = _header(parsed, resolved, Path(schema).stem)
	parts, why = _only(resolved, struct, header)
	assert why is None, why

	mine = [part for part in parts if part.placement.name == member]
	assert mine and mine[0].role == build.REPEAT, [
		(part.placement.name, part.role) for part in parts]
	if shape == "fixed":
		assert mine[0].digits == 2
	if shape == "delimiter":
		assert mine[0].delimiter == b")"


def test_a_counted_run_writes_the_count_the_walk_found() -> None:
	"""ble's `num` and edges' `count` are element counts, not byte lengths.

	Both schemas say so -- "a LITERAL count of elements that have no
	single size" -- so a struct run's size field cannot be written the way
	a byte span's is. It is the walk that knows the number, which is why
	the walk happens at the count field rather than at the run.

	And the count is therefore NOT a caller parameter: a caller able to
	pass both a count and the elements is one able to make them disagree.
	"""
	parsed, resolved = _parts(ROOT / "example" / "ble" / "ble.situ")
	header = _header(parsed, resolved, "ble")
	parts, why = _only(resolved, "le_advertising_report", header)
	assert why is None, why

	num = [part for part in parts if part.placement.name == "num"][0]
	assert num.role == build.SIZE and num.inner == "situ_adv_report"

	text = build.generate(parsed, resolved, "ble", "situ",
	                      header)["ble_build.h"]
	head = text.split("situ_le_advertising_report_build(")[1].split("{")[0]
	assert "num" not in head, f"the count is a parameter: {head}"
	assert f"{build.OUT}[{build.AT}] = (uint8_t){build.COUNT}" in text


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_counted_run_is_built_by_composing_its_elements(
		tmp_path: Path) -> None:
	"""Two `adv_report`s built separately, then composed into the run.

	The control is the short blob: one byte fewer than two whole elements
	is not a whole number of them, and only the walk can say so -- the
	count field would otherwise be written from a number nobody checked.
	"""
	parsed, resolved = _parts(ROOT / "example" / "ble" / "ble.situ")
	files = dict(build.generate(parsed, resolved, "ble", "situ",
	                            _header(parsed, resolved, "ble")))
	files.update(generate_c(parsed, resolved, "ble").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include <string.h>
#include "ble_build.h"

int main(void)
{
	uint8_t     one[64], two[64], run[192], whole[256];
	uint32_t    n1 = 0, n2 = 0, wrote = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	const uint8_t addr[6] = { 0x66, 0x55, 0x44, 0x33, 0x22, 0x11 };
	const uint8_t ad[2]   = { 0x02, 0x01 };

	if (situ_adv_report_build(one, sizeof one, 0, 0, addr, ad, 2, -40, &n1)
	    != SITU_OK) return 2;
	if (situ_adv_report_build(two, sizeof two, 2, 1, addr, ad, 2, -50, &n2)
	    != SITU_OK) return 3;
	memcpy(run, one, n1);
	memcpy(run + n1, two, n2);

	err = situ_le_advertising_report_build(whole, sizeof whole, 0x3E,
	                                       (uint8_t)(2u + n1 + n2), 0x02,
	                                       run, n1 + n2, &wrote);
	printf("%d %u %u\\n", (int)err, wrote,
	       err == SITU_OK ? whole[3] : 0u);
	if (err != SITU_OK) return 4;

	situ_msg_init(&msg, whole, wrote);
	printf("%d\\n",
	       (int)situ_le_advertising_report_view(&msg, 0, wrote, &view));

	/* one byte short of two whole elements */
	printf("%d\\n", (int)situ_le_advertising_report_build(whole,
	        sizeof whole, 0x3E, (uint8_t)(1u + n1 + n2), 0x02, run,
	        n1 + n2 - 1u, &wrote));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "ble.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, f"exited {ran.returncode}: {ran.stderr}"
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 28 2", \
		f"two reports, 28 bytes, and a count of 2: {lines[0]}"
	assert lines[1] == "0", f"the result does not acquire a view: {lines[1]}"
	assert lines[2] == "2", \
		f"a blob that is not whole elements must be refused: {lines[2]}"


#: A delimited run whose element is FIXED size, so `_required` always
#: measures it and the zero-progress guard can never fire. That is what
#: makes it the only fixture the boundary check alone answers -- sexpr's own
#: case is caught by both, so neither sabotage goes red on it.
BAG = """struct cell {
\tu8  a;
\tu8  b;
}

struct bag {
\tu8    open;
\tcell  cells[] until ")";
}
"""

#: A run of a struct that is zero bytes long, which `_required` measures as
#: zero. No delimiter, so only the zero-progress guard can refuse it.
HEAP = """struct empty {
}

struct heap {
\tu8     n;
\tempty  items[n];
}
"""


def _compile(tmp_path: Path, stem: str, body: str,	# type: ignore[no-untyped-def]
		probe: str):
	"""Generate, write and compile an inline schema with a probe."""
	parsed, resolved = _inline(body)
	files = dict(build.generate(parsed, resolved, stem, "situ",
	                            _header(parsed, resolved, stem)))
	files.update(generate_c(parsed, resolved, stem).files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")
	(tmp_path / "probe.c").write_text(probe, encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / f"{stem}.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	return tmp_path / "probe"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_delimiter_at_an_element_boundary_is_refused(
		tmp_path: Path) -> None:
	"""A struct run cannot be scanned the way a byte run is.

	A byte run refuses its delimiter anywhere in it. A struct run must
	not: a `)` inside a nested element is legitimate, and the reader walks
	structurally. What breaks it is a delimiter at an ELEMENT BOUNDARY,
	where the reader stops early -- so the check is at each boundary the
	walk reaches, which is where the reader looks.

	Measured with the check removed: the second cell beginning with `)` is
	written and accepted, and a reader then sees a one-element run. The
	fixture is constructed because sexpr's own case is caught by the
	zero-progress guard as well, so on that one neither sabotage is red.
	"""
	probe = _compile(tmp_path, "bag", BAG, """
#include <stdio.h>
#include "bag_build.h"

int main(void)
{
	uint8_t     out[32];
	uint32_t    wrote = 0;
	situ_err_t  err;
	const uint8_t good[4] = { 'a', 'b', 'c', 'd' };
	const uint8_t bad[4]  = { 'a', 'b', ')', 'd' };

	err = situ_bag_build(out, sizeof out, '(', good, 4, &wrote);
	printf("%d %u\\n", (int)err, wrote);
	printf("%d\\n", (int)situ_bag_build(out, sizeof out, '(', bad, 4,
	                                   &wrote));
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0 6", f"two cells and the delimiter: {lines[0]}"
	assert lines[1] == "2", \
		f"an element beginning with the delimiter must be refused: {lines[1]}"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_an_element_that_measures_zero_stops_the_walk(
		tmp_path: Path) -> None:
	"""What makes a generated loop terminate, with a case that needs it.

	A generated loop is somebody else's unattended program, so it says
	what stops it. `empty` is zero bytes, its `_required` measures zero,
	and without this check the walk never advances: measured, the probe
	runs until `timeout` kills it at exit 124 rather than returning.

	No delimiter is involved, so the boundary check cannot cover this one
	-- which is what makes it the guard's own fixture rather than a second
	assertion about a case something else already refuses.
	"""
	probe = _compile(tmp_path, "heap", HEAP, """
#include <stdio.h>
#include "heap_build.h"

int main(void)
{
	uint8_t     out[32];
	uint32_t    wrote = 0;
	const uint8_t items[2] = { 0, 0 };

	printf("%d\\n", (int)situ_heap_build(out, sizeof out, NULL, 0, &wrote));
	printf("%d\\n", (int)situ_heap_build(out, sizeof out, items, 2,
	                                    &wrote));
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=30)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0", f"a run of no elements is legal: {lines[0]}"
	assert lines[1] == "2", \
		f"an element that measures zero must be refused: {lines[1]}"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_delimited_run_of_structs_round_trips(tmp_path: Path) -> None:
	"""sexpr's `list`, which is what hull's "opening and closing a list"
	needs, built from element bytes and read back.

	`items_count` answers 3 for `(a b )` rather than 2, and that is hull's
	finding 1 -- a list closed after whitespace gains a phantom element --
	which is the reader's and is recorded against `example/sexpr`. It is
	asserted here as it stands so that fixing it turns this red rather
	than leaving a stale expectation behind.
	"""
	parsed, resolved = _parts(ROOT / "example" / "sexpr" / "sexpr.situ")
	files = dict(build.generate(parsed, resolved, "sexpr", "situ",
	                            _header(parsed, resolved, "sexpr")))
	files.update(generate_c(parsed, resolved, "sexpr").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")
	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "sexpr_build.h"

int main(void)
{
	uint8_t     out[64];
	uint32_t    wrote = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;
	const uint8_t items[4] = { 'a', ' ', 'b', ' ' };

	err = situ_list_build(out, sizeof out, '(', items, 4, &wrote);
	printf("%d %u ", (int)err, wrote);
	for (i = 0; i < wrote; i++) putchar(out[i]);
	printf("\\n");
	if (err != SITU_OK) return 2;

	situ_msg_init(&msg, out, wrote);
	err = situ_list_view(&msg, 0, wrote, &view);
	printf("%d\\n", (int)err);
	if (err != SITU_OK) return 3;
	printf("%u\\n", situ_list_items_count(view));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "sexpr.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0 6 (a b )", lines[0]
	assert lines[1] == "0", f"the list does not acquire a view: {lines[1]}"
	assert lines[2] == "3", \
		f"hull's finding 1, the phantom element, still stands: {lines[2]}"


def test_a_variant_gets_one_builder_per_arm() -> None:
	"""Which arm is said by which builder, not by a parameter.

	So the discriminant is the function's to write and cannot disagree
	with the body -- the same argument as computing a size field rather
	than asking for it. modbus's `request` switches on a `function_code`
	enum, and `read_coils` is 1.
	"""
	parsed, resolved = _parts(ROOT / "example" / "modbus" / "modbus.situ")
	header = _header(parsed, resolved, "modbus")
	cases  = {build.arm_name(case): case
	          for case in build.arms(resolved.structs["request"])}
	assert "read_coils" in cases and "default" in cases

	parts, why = _only(resolved, "request", header, cases["read_coils"])
	assert why is None, why
	tag = [part for part in parts if part.role == build.ARMTAG]
	assert len(tag) == 1 and tag[0].value == 1, [
		(part.placement.name, part.role) for part in parts]

	text = build.generate(parsed, resolved, "modbus", "situ",
	                      header)["modbus_build.h"]
	head = text.split("situ_request_build_read_coils(")[1].split("{")[0]
	assert "function" not in head, \
		f"the discriminant is a parameter, so it can disagree: {head}"


def test_the_default_arm_refuses_a_value_a_named_arm_claims() -> None:
	"""The default builder DOES take the discriminant, because the schema
	names no value for it -- and a value another arm claims would make the
	message read as that arm, so it is refused.

	The named builders are where those values are written.
	"""
	parsed, resolved = _parts(ROOT / "example" / "modbus" / "modbus.situ")
	header = _header(parsed, resolved, "modbus")
	cases  = {build.arm_name(case): case
	          for case in build.arms(resolved.structs["request"])}

	parts, why = _only(resolved, "request", header, cases["default"])
	assert why is None, why
	disc = [part for part in parts if part.others]
	assert len(disc) == 1, [(part.placement.name, part.role)
	                        for part in parts]
	assert 1 in disc[0].others and 23 in disc[0].others


def test_a_peeked_discriminant_is_written_by_the_arm() -> None:
	"""A peek does not consume, so those bytes belong to the arm.

	Writing them for the discriminant too would write them twice -- and
	nothing in `_validate` could see it, because the result would simply
	be a different, valid message. So the discriminant writes nothing and
	the ARM carries the check that its first byte says which arm this is.
	"""
	parsed, resolved = _parts(ROOT / "example" / "sexpr" / "sexpr.situ")
	header = _header(parsed, resolved, "sexpr")
	cases  = {build.arm_name(case): case
	          for case in build.arms(resolved.structs["sexpr"])}

	parts, why = _only(resolved, "sexpr", header, cases["as_list"])
	assert why is None, why
	roles = [part.role for part in parts]
	assert roles == [build.PEEKED, build.NESTED], roles
	assert parts[0].placement.name == "kind"

	text = build.generate(parsed, resolved, "sexpr", "situ",
	                      header)["sexpr_build.h"]
	assert "if (as_list[0] != 40u)" in text, \
		"the arm's first byte is not checked against its own case"
	assert "if (as_symbol[0] ==" not in text, \
		"`as_symbol` should be refused outright, not emitted"


def test_an_arm_that_ends_before_a_byte_it_does_not_own_is_refused() -> None:
	"""`sexpr.as_symbol` was accepted at plan time and refused every input.

	A `symbol` is `u8 name[] before ' ' | '(' | ')'`, so it has a
	`_required` -- `frames_itself` said yes -- and that function can never
	answer the length a caller passes: with the delimiter absent it
	reports TRUNCATED, and with it present the extent stops short of it,
	because a `before` run does not consume. The parent owns the byte that
	ends it.

	Found by building one, which is the only way this shows: every static
	check said it was fine.
	"""
	parsed, resolved = _parts(ROOT / "example" / "sexpr" / "sexpr.situ")
	header = _header(parsed, resolved, "sexpr")
	cases  = {build.arm_name(case): case
	          for case in build.arms(resolved.structs["sexpr"])}

	_, why = _only(resolved, "sexpr", header, cases["as_symbol"])
	assert why is not None and "handed over as bytes" in why, why

	assert build.frames_itself(header, "situ_symbol"), \
		"the header does declare a `_required` for it, which is the trap"
	assert not build.hands_over(header, "symbol", "situ", resolved.structs)


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_nested_s_expression_is_built_by_its_own_builders(
		tmp_path: Path) -> None:
	"""`(())`, every byte of it written by generated code.

	An empty list, that list as a `sexpr` element, a list holding that
	element, and the whole thing as a `sexpr` -- four builders composed,
	which is what hull's "opening and closing a list" asked for.

	The last line is the control for the arm's first-byte check: the same
	bytes asked for as a text are a VALID list, so `_validate` accepts
	them and only that check can refuse.
	"""
	parsed, resolved = _parts(ROOT / "example" / "sexpr" / "sexpr.situ")
	files = dict(build.generate(parsed, resolved, "sexpr", "situ",
	                            _header(parsed, resolved, "sexpr")))
	files.update(generate_c(parsed, resolved, "sexpr").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "sexpr_build.h"

int main(void)
{
	uint8_t     inner[16], elem[16], outer[16], top[16];
	uint32_t    n1 = 0, n2 = 0, n3 = 0, n4 = 0, n5 = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    i;

	if (situ_list_build(inner, sizeof inner, '(', NULL, 0, &n1) != SITU_OK)
		return 2;
	if (situ_sexpr_build_as_list(elem, sizeof elem, inner, n1, &n2)
	    != SITU_OK) return 3;
	if (situ_list_build(outer, sizeof outer, '(', elem, n2, &n3) != SITU_OK)
		return 4;
	err = situ_sexpr_build_as_list(top, sizeof top, outer, n3, &n4);
	printf("%d %u ", (int)err, n4);
	for (i = 0; i < n4; i++) putchar(top[i]);
	printf("\\n");
	if (err != SITU_OK) return 5;

	situ_msg_init(&msg, top, n4);
	printf("%d\\n", (int)situ_sexpr_view(&msg, 0, n4, &view));

	/* a valid list, asked for as a text */
	printf("%d\\n", (int)situ_sexpr_build_as_text(top, sizeof top, outer,
	                                             n3, &n5));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "sexpr.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, f"exited {ran.returncode}: {ran.stderr}"
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 4 (())", f"a nested list, four bytes: {lines[0]}"
	assert lines[1] == "0", f"the result does not acquire a view: {lines[1]}"
	assert lines[2] == "2", \
		f"a list asked for as a text must be refused: {lines[2]}"


#: Two arms of ONE type behind a peeked discriminant, so the arm's own
#: `_required` accepts either input and nothing upstream can refuse. That
#: is what makes it the only fixture the kind checks answer alone: against
#: `example/sexpr` the wrong-arm call is caught by `text_required` as well,
#: so neither sabotage goes red there.
PICK = """struct two {
\tu8  k;
\tu8  x;
}

struct pick {
\tpeek u8  kind;
\tvariant  body switch (kind) {
\t\tcase 'a': two  as_a;
\t\tdefault:  two  as_b;
\t}
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_peeked_arm_is_checked_against_the_case_it_was_called_for(
		tmp_path: Path) -> None:
	"""Four calls, and the two that must be refused are refused by nothing
	else.

	A peeked discriminant is not written -- those bytes belong to the arm
	-- so a caller handing `b`-kind bytes to the `'a'` builder would get
	SITU_OK about a message that reads as the OTHER arm. `_validate`
	cannot object: it is a valid `pick`, just not the one asked for.

	The default arm is the mirror: it takes no case value, so its bytes
	must not begin with one a named arm claims.
	"""
	probe = _compile(tmp_path, "pick", PICK, """
#include <stdio.h>
#include "pick_build.h"

int main(void)
{
	uint8_t     out[16];
	uint32_t    n = 0;
	const uint8_t a[2] = { 'a', 'z' };
	const uint8_t b[2] = { 'b', 'z' };

	printf("%d %d %d %d\\n",
	       (int)situ_pick_build_as_a(out, sizeof out, a, 2, &n),
	       (int)situ_pick_build_as_a(out, sizeof out, b, 2, &n),
	       (int)situ_pick_build_as_b(out, sizeof out, b, 2, &n),
	       (int)situ_pick_build_as_b(out, sizeof out, a, 2, &n));
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split("\n")[0] == "0 2 0 2", (
		"the four calls are: right bytes to the named arm, wrong bytes to "
		"the named arm, right bytes to the default, and a named arm's kind "
		f"to the default -- got {ran.stdout.split(chr(10))[0]}")


#: Two packed fields and no constraint on either, which is what makes it
#: the fixture the range check answers for: `example/ntp`'s own over-range
#: version is refused by its enum as well, so neither sabotage is red
#: there. `situ_bits_set_*` MASKS, so without the check 9 lands as 1.
PACKED = """bit_order msb_first;

struct pk {
\tu3  a;
\tu5  b;
}
"""


def test_a_byte_several_members_share_is_one_group() -> None:
	"""A packed group is zeroed once and then set field by field.

	ntp's first byte is leap, version and mode -- 2, 3 and 3 bits. The
	group opens on the first and closes on the last, so the capacity
	check, the zeroing and the cursor advance each happen once however
	many fields share the byte.
	"""
	parsed, resolved = _parts(ROOT / "example" / "ntp" / "ntp.situ")
	header = _header(parsed, resolved, "ntp")
	parts, why = _only(resolved, "ntp_packet", header)
	assert why is None, why

	bits = [part for part in parts if part.role == build.BITS]
	assert [part.placement.name for part in bits] == ["leap", "version",
	                                                  "mode"]
	assert [part.bit_at for part in bits] == [0, 2, 5]
	assert [part.bytes for part in bits] == [1, 1, 1]
	assert bits[0].opens and not bits[1].opens and not bits[2].opens
	assert bits[2].closes and not bits[0].closes

	text = build.generate(parsed, resolved, "ntp", "situ",
	                      header)["ntp_build.h"]
	body = text.split("situ_ntp_packet_build(")[1]
	assert body.count(f"memset({build.OUT} + {build.AT}, 0, 1u)") == 1
	# The ORDER is `traverse.bit_extractor`'s, which is also what
	# `owned.py` asks: two derivations of which way the bits run is how a
	# little-endian u24 came to be read one way and written the other.
	assert "situ_bits_set_msb" in body


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_packed_fields_land_where_the_reader_looks(tmp_path: Path) -> None:
	"""ntp's first byte, built and read back through the accessors.

	`leap = 0`, `version = 4`, `mode = 3` is `0b00100011` -- 0x23 -- which
	is the byte NTP puts on the wire, so this checks the packing against
	the format rather than against the writer's own arithmetic.
	"""
	parsed, resolved = _parts(ROOT / "example" / "ntp" / "ntp.situ")
	files = dict(build.generate(parsed, resolved, "ntp", "situ",
	                            _header(parsed, resolved, "ntp")))
	files.update(generate_c(parsed, resolved, "ntp").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include <string.h>
#include "ntp_build.h"

int main(void)
{
	uint8_t     out[64], ts[8];
	uint32_t    n = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;

	memset(ts, 0, sizeof ts);
	err = situ_ntp_packet_build(out, sizeof out, 0, 4, 3, 2, 6, -20,
	                            0, 0, 0, ts, 8, ts, 8, ts, 8, ts, 8, &n);
	printf("%d %u %02X\\n", (int)err, n, err == SITU_OK ? out[0] : 0);
	if (err != SITU_OK) return 2;

	situ_msg_init(&msg, out, n);
	if (situ_ntp_packet_view(&msg, 0, &view) != SITU_OK) return 3;
	printf("%u %u %u\\n",
	       (unsigned)situ_ntp_packet_leap_get(view),
	       (unsigned)situ_ntp_packet_version_get(view),
	       (unsigned)situ_ntp_packet_mode_get(view));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "ntp.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0 48 23", f"NTP's first byte is 0x23: {lines[0]}"
	assert lines[1] == "0 4 3", f"and reads back as given: {lines[1]}"


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_value_wider_than_its_field_is_refused_not_masked(
		tmp_path: Path) -> None:
	"""`situ_bits_set_*` masks, so without a check 9 lands in three bits
	as 1 -- a valid message saying something else, which `_validate` has
	nothing to object to.

	Measured with the check removed: this fixture returns SITU_OK. ntp's
	own over-range `version` is refused by its enum as well, so it could
	not have answered for this.
	"""
	probe = _compile(tmp_path, "pk", PACKED, """
#include <stdio.h>
#include "pk_build.h"

int main(void)
{
	uint8_t     out[8];
	uint32_t    n = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;

	err = situ_pk_build(out, sizeof out, 5, 9, &n);
	printf("%d %u %02X\\n", (int)err, n, err == SITU_OK ? out[0] : 0);
	if (err != SITU_OK) return 2;
	situ_msg_init(&msg, out, n);
	if (situ_pk_view(&msg, 0, &view) != SITU_OK) return 3;
	printf("%u %u\\n", (unsigned)situ_pk_a_get(view),
	       (unsigned)situ_pk_b_get(view));
	printf("%d\\n", (int)situ_pk_build(out, sizeof out, 9, 0, &n));
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0 1 A9", f"5 then 9 msb-first is 0xA9: {lines[0]}"
	assert lines[1] == "5 9", f"and reads back as given: {lines[1]}"
	assert lines[2] == "2", \
		f"9 does not fit three bits and must be refused: {lines[2]}"
