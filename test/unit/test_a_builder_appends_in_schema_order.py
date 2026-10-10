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


def _shape(parsed, resolved, header: str = ""):	# type: ignore[no-untyped-def]
	"""What `situc build` passes, so a test asks the same question."""
	return build.Shape(header, "situ", resolved.structs,
	                   {decl.name: decl for decl in parsed.varints()})


def _only(resolved, name: str, header: str = "",	# type: ignore[no-untyped-def]
		arm: Arm | None = None, parsed=None):
	struct = resolved.structs[name]
	shape = build.Shape(header, "situ", resolved.structs,
	                    {decl.name: decl for decl in parsed.varints()}
	                    if parsed is not None else None)
	return build.plan(struct, shape, arm)


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
	parsed, resolved = _parts(ROOT / "example" / "png" / "png.situ")
	chunk = resolved.structs["chunk"]
	assert traverse.append_only_refusals(chunk) == []

	# And it BUILDS, which is the stronger form of the same statement:
	# the trailing CRC is reserved where the writer reaches it and filled
	# once the message is whole. It used to be refused here for a second
	# reason -- that nothing computed a checksum -- and this test asserted
	# that refusal; the refusal is gone and the property it was standing
	# in for is the one left.
	header = _header(parsed, resolved, "png")
	parts, why = _only(resolved, "chunk", header)
	assert why is None, why
	assert any(part.role == build.TAG for part in parts), [
		(part.placement.name, part.role) for part in parts]


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
	shape   = _shape(parsed, resolved, header)
	built   = {struct.name for struct in build.buildable(resolved, shape)}
	refused = dict(build.refusals(resolved, shape))

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
	# Escaping would write bytes other than the ones the caller gave, and
	# the reader's unescape has no writer to be tested against.
	('u8 x[] until "\\"" [escape = "\\\\"]', "escapes its delimiter"),
	# Trimming is the same: the bytes it holds are not the bytes given.
	('u8 x[] until " " [trim = " "]', "is trimmed"),
])
def test_a_delimiter_the_writer_cannot_place_is_refused(
		member: str, wanted: str) -> None:
	"""The shapes a run's delimiter can still not be written in.

	`\r\n` was in this list and is not any more: a multi-byte delimiter
	is written now, and the row was removed rather than reworded because
	there is no refusal left to assert. What remains is an ambiguity the
	schema has (several delimiters) and two transformations of the
	caller's bytes (escaping, trimming).
	"""
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

	**This test asserted a misdiagnosis for a day.** `edge_varint` is a
	varint TYPE, and the refusal it checked for -- "a nested
	`edge_varint`" -- called it a nested struct, which is what the planner
	said before varints had a branch of their own. A test that pins a
	message pins whatever the message says, including the part that is
	wrong, so this one was holding the fault in place.

	What it checks now is the refusal that applies: the runtime has one
	varint encoder and it is LEB128, so a big-endian varint is refused by
	name rather than written by a second implementation here.

	`mqtt_string` keeps the original point -- the header's `_required` is
	what `hands_over` reads -- which is why that half stays.
	"""
	parsed, resolved = _parts(ROOT / "test" / "schema" / "edges.situ")
	header = _header(parsed, resolved, "edges")
	_, why = _only(resolved, "varint_driver", header, None, parsed)
	assert why is not None and "varint" in why, why
	assert "the runtime writes LEB128" in why, why

	# And the predicate reads the artifact rather than guessing.
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
	"""sexpr's `list`, built from element bytes and read back.

	Two cases, and the second is hull's finding 1 arriving on the write
	side. `a b` builds `(a b)` and reads back as two items. `a b ` -- the
	same with a trailing space -- is REFUSED, because that space makes a
	third, empty element which the schema does not accept: their finding
	says a list closed after whitespace gains a phantom element, and since
	the walk validates every element the phantom is what refuses the
	build.

	So a caller cannot write a list whose items would read back with an
	element they did not intend, which is a stronger guarantee than the
	reader gives on its own.
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
	uint32_t    n = 0, i;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	const uint8_t tight[3] = { 'a', ' ', 'b' };
	const uint8_t loose[4] = { 'a', ' ', 'b', ' ' };

	err = situ_list_build(out, sizeof out, '(', tight, 3, &n);
	printf("%d %u ", (int)err, n);
	for (i = 0; i < n; i++) putchar(out[i]);
	printf("\\n");
	if (err != SITU_OK) return 2;

	situ_msg_init(&msg, out, n);
	if (situ_list_view(&msg, 0, n, &view) != SITU_OK) return 3;
	printf("%u\\n", situ_list_items_count(view));

	/* the same with a trailing space, which makes a phantom element */
	printf("%d\\n", (int)situ_list_build(out, sizeof out, '(', loose, 4,
	                                    &n));
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

	assert lines[0] == "0 5 (a b)", lines[0]
	assert lines[1] == "2", f"two items, not the phantom three: {lines[1]}"
	assert lines[2] != "0", \
		"a trailing space makes a phantom element and must be refused"



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
	# The message says WHICH of the two reasons applies. It used to hedge
	# -- "either its own bytes do not state its extent, or it ends
	# `before` a byte it does not own" -- while the code knew every time:
	# measured over the corpus, fifteen were the first and two the second.
	assert why is not None and "ends `before` a byte it does not own" in why, \
		why

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


#: An element with a constraint and nothing else interesting. `n = 9`
#: MEASURES fine -- two bytes, like any other element -- and its own
#: `_validate` refuses it, which is the whole of hull's finding 17.
CONSTRAINED = """struct el {
\tu8  n [max = 3];
\tu8  pad;
}

struct bag {
\tu8   open;
\tel   items[] until ")";
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_an_element_the_schema_refuses_is_not_built(tmp_path: Path) -> None:
	"""Measuring an element is not validating it.

	`_required` answers how long one is, and the whole-message `_validate`
	does not reach a run's elements -- "an array's elements are validated
	by the caller that chooses to walk them", and the caller choosing to
	walk them is this writer's loop. So a run of elements the schema
	refuses was built and reported SITU_OK while every element's own
	`_validate` said CONSTRAINT.

	Reported by hull as finding 17, against their canonical s-expressions:
	`01:a` went through against `[minimal]`, and so did an atom whose list
	was never closed. Their finding 3 is the same division seen from the
	reader's side.

	The element's own verdict is returned rather than a flat CONSTRAINT,
	so a caller is told which failure it was.
	"""
	probe = _compile(tmp_path, "bag", CONSTRAINED, """
#include <stdio.h>
#include "bag_build.h"

int main(void)
{
	uint8_t     out[32];
	uint32_t    n = 0;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	const uint8_t good[2] = { 3, 0 };
	const uint8_t bad[2]  = { 9, 0 };

	printf("%d\\n", (int)situ_bag_build(out, sizeof out, '(', good, 2, &n));
	err = situ_bag_build(out, sizeof out, '(', bad, 2, &n);
	printf("%d\\n", (int)err);

	/* and what the element itself says about those bytes, which is what
	 * the build now reports */
	situ_msg_init(&msg, (uint8_t *)(uintptr_t)bad, 2);
	if (situ_el_view(&msg, 0, &view) != SITU_OK) return 2;
	printf("%d\\n", (int)situ_el_validate(view));
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0", f"a conforming element builds: {lines[0]}"
	assert lines[1] == "2", \
		f"one the schema refuses must not: {lines[1]}"
	assert lines[1] == lines[2], (
		"the element's own verdict is what the build reports, so a caller "
		f"learns which failure it was: {lines[1]} vs {lines[2]}")


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_trailing_checksum_is_computed_and_proved(tmp_path: Path) -> None:
	"""PNG's chunk, built with its CRC, against a vector situ did not
	produce.

	`0000000049454E44AE426082` is the IEND chunk every PNG file ends
	with: length 0, "IEND", and the CRC `AE426082` that the PNG
	specification states. A round trip through situ's own `_check` would
	only say the writer agrees with itself; this says it agrees with PNG.
	The second case is checked against `zlib` in the assertion below,
	which is the same argument twice over.

	`validate` deliberately does not verify a checksum -- "the coverage
	may run to the end of the message, and a constraint walk that costs a
	file read is not the flat model 0051 settled on. Call it where the
	caller has the whole message" -- and the writer is that caller, which
	is 26.620's argument at a second construct.

	The tag's bytes are reserved when the writer reaches them and filled
	once the message is whole, and the store is then PROVED by the
	generated `_check` rather than asserted: a wrong byte order refuses
	the build instead of emitting a chunk whose own reader rejects it.
	"""
	parsed, resolved = _parts(ROOT / "example" / "png" / "png.situ")
	files = dict(build.generate(parsed, resolved, "png", "situ",
	                            _header(parsed, resolved, "png")))
	files.update(generate_c(parsed, resolved, "png").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	# The codec's own implementation, which `_compute` calls: a CRC is
	# `impl crc32 derived`, so the kernel is generated rather than linked
	# from the runtime.
	from situc.codegen.c import derived
	(tmp_path / "png_derived.c").write_text(
		derived.generate(parsed, "png"), encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "png_build.h"

int main(void)
{
	uint8_t     out[64];
	uint32_t    n = 0, i;
	situ_err_t  err;
	const uint8_t iend[4] = { 'I', 'E', 'N', 'D' };
	const uint8_t idat[4] = { 'I', 'D', 'A', 'T' };
	const uint8_t data[3] = { 1, 2, 3 };

	err = situ_chunk_build(out, sizeof out, iend, NULL, 0, &n);
	printf("%d ", (int)err);
	for (i = 0; i < n; i++) printf("%02X", out[i]);
	printf("\\n");

	err = situ_chunk_build(out, sizeof out, idat, data, 3, &n);
	printf("%d ", (int)err);
	for (i = 0; i < n; i++) printf("%02X", out[i]);
	printf("\\n");
	return 0;
}
""", encoding="ascii")

	sources = [str(tmp_path / "probe.c"), str(tmp_path / "png.c"),
	           str(RUNTIME / "situ.c")]
	derived_c = tmp_path / "png_derived.c"
	if derived_c.exists():
		sources.insert(2, str(derived_c))

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}", *sources,
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")

	# The vector, from PNG rather than from situ.
	assert lines[0] == "0 0000000049454E44AE426082", lines[0]

	# And the second checked against zlib, which knows nothing of situ.
	import zlib
	want = zlib.crc32(b"IDAT\x01\x02\x03")
	assert lines[1] == f"0 0000000349444154010203{want:08X}", (
		f"{lines[1]} against zlib's {want:08X}")


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_sixteen_bit_sum_is_stored_in_the_codec_s_order(
		tmp_path: Path) -> None:
	"""The other width, and the one with no external constant to check.

	PNG's CRC has a vector the specification states; a `summing` codec
	over sixteen bits has none, so the generated `_check` is what stands
	between a wrong store and a message whose own reader rejects it. It is
	demonstrably capable rather than merely present: with the byte order
	deliberately flipped in the emitter, this case comes back
	`SITU_ERR_CHECKSUM` (8) instead of building.

	The assertion is on the bytes for the same reason as PNG's -- a
	round trip through `_check` alone would say only that the writer
	agrees with itself.
	"""
	parsed, resolved = _parts(ROOT / "test" / "schema" / "edges.situ")
	files = dict(build.generate(parsed, resolved, "edges", "situ",
	                            _header(parsed, resolved, "edges")))
	files.update(generate_c(parsed, resolved, "edges").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	from situc.codegen.c import derived
	(tmp_path / "edges_derived.c").write_text(
		derived.generate(parsed, "edges"), encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "edges_build.h"

int main(void)
{
	uint8_t     out[32];
	uint32_t    n = 0, i;
	situ_err_t  err = situ_inner_body_build(out, sizeof out, 0x1234, &n);

	printf("%d ", (int)err);
	for (i = 0; i < n; i++) printf("%02X", out[i]);
	printf("\\n");
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "edges.c"),
		 str(tmp_path / "edges_derived.c"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	# 0x1234 then its sum, which for a single u16 is itself -- stored
	# big-endian, so `12341234` and not `12343412`.
	assert ran.stdout.split("\n")[0] == "0 12341234", ran.stdout


#: One varint and two bytes after it, so the encoded width is visible in
#: the output. `leb128` with a 28-bit bound is MQTT's remaining length.
VARINT = """varint_type vlen {
\tencoding  = leb128;
\tmax_bits  = 28;
\tmax_bytes = 4;
}

struct v {
\tvlen  n;
\tu8    rest[2];
}
"""


def test_a_varint_is_not_a_nested_struct() -> None:
	"""`mqtt.packet.length` is a `remaining_length`, which is a varint type.

	It has no `scalar` and a `type_name` that names no struct, so it fell
	into the nested-struct branch and was refused as "a nested
	`remaining_length`" -- fifteen refusals naming a construct it is not.
	The fourth misdiagnosis of this family in two days, and the same cause
	every time: a branch ordered by what a member LACKS rather than by
	what it is.
	"""
	parsed, resolved = _parts(ROOT / "example" / "mqtt" / "mqtt.situ")
	header = _header(parsed, resolved, "mqtt")
	cases  = {build.arm_name(case): case
	          for case in build.arms(resolved.structs["packet"])}
	parts, why = _only(resolved, "packet", header, cases["connect"], parsed)
	assert why is None, why

	length = [part for part in parts if part.placement.name == "length"]
	assert length and length[0].role == build.VARINT, [
		(part.placement.name, part.role) for part in parts]
	assert length[0].value == 28, "the schema's own bound on the value"


def test_a_big_endian_varint_is_refused_by_name() -> None:
	"""`situ_varint_put` is the only encoder the runtime has.

	The big-endian form has a reader and a length function and no writer,
	so sqlite's and edges' varints are refused rather than written by a
	second implementation here -- which is the fault `owned.py` paid for
	in BCD, where an encode and a decode were self-consistent and wrong
	together.
	"""
	parsed, resolved = _parts(ROOT / "example" / "sqlite" / "sqlite.situ")
	header = _header(parsed, resolved, "sqlite")
	shape  = _shape(parsed, resolved, header)
	named  = [why for _, why in build.refusals(resolved, shape)
	          if "varint" in why]
	assert named, "no sqlite struct is refused for its varint any more"
	assert any("the runtime writes LEB128" in why for why in named), named


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_a_varint_is_written_in_the_encoding_the_spec_states(
		tmp_path: Path) -> None:
	"""A vector table from LEB128 rather than from situ.

	`321` is MQTT 2.2.3's own worked example and comes out `C1 02`; the
	others are the boundaries where the encoded width changes, which is
	what a length-prefixed format gets wrong first. One past the schema's
	28-bit bound is refused rather than written in five bytes, which is
	the standard's "the receiver MUST close the network connection" seen
	from the writing side.
	"""
	probe = _compile(tmp_path, "vi", VARINT, """
#include <stdio.h>
#include "vi_build.h"

static void show(uint64_t n)
{
	uint8_t     out[16];
	uint32_t    w = 0, i;
	const uint8_t rest[2] = { 0xAA, 0xBB };
	situ_err_t  err = situ_v_build(out, sizeof out, n, rest, &w);

	printf("%d ", (int)err);
	for (i = 0; i < w; i++) printf("%02X", out[i]);
	printf("\\n");
}

int main(void)
{
	show(0);
	show(127);
	show(128);
	show(321);
	show(268435455u);
	show(268435456u);
	return 0;
}
""")
	ran = subprocess.run([str(probe)], capture_output=True, text=True,
	                     cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, ran.stderr
	lines = ran.stdout.split("\n")
	assert lines[0] == "0 00AABB", lines[0]
	assert lines[1] == "0 7FAABB", f"127 is one byte: {lines[1]}"
	assert lines[2] == "0 8001AABB", f"128 is two: {lines[2]}"
	assert lines[3] == "0 C102AABB", f"MQTT's own example for 321: {lines[3]}"
	assert lines[4] == "0 FFFFFF7FAABB", f"the 28-bit maximum: {lines[4]}"
	assert lines[5] == "2 ", f"one past it is refused: {lines[5]}"


def test_a_two_byte_delimiter_is_one_delimiter() -> None:
	"""`\r\n` is every line in http and smtp.

	The single-byte and multi-byte forms are one emitter, because the
	one-byte case is the other with N = 1 and a second comparison written
	for it is a second thing to be wrong. What stays refused in http is
	the member that ends at TWO delimiters, which is an ambiguity in the
	schema rather than a gap in the writer.
	"""
	parsed, resolved = _parts(ROOT / "example" / "http" / "http.situ")
	header = _header(parsed, resolved, "http")
	parts, why = _only(resolved, "request_line", header, None, parsed)
	assert why is None, why

	version = [part for part in parts
	           if part.placement.name == "version"][0]
	assert version.role == build.ENDED
	assert version.delimiter == b"\r\n"

	refused = dict(build.refusals(resolved, _shape(parsed, resolved,
	                                               header)))
	assert "header_field" in refused
	assert "any of 2 delimiters" in refused["header_field"]


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_an_http_request_head_is_built_line_by_line(tmp_path: Path) -> None:
	"""A real HTTP message head, every byte from generated code.

	`GET / HTTP/1.1\r\nHost: example.invalid\r\n\r\n` -- the line from
	`request_line_build`, the field handed over as bytes because a
	`header_field` ends at two delimiters and is refused, and the blank
	line written by the run's own delimiter. 41 bytes, and it acquires a
	view.

	The third call is the control for the needle being a SEQUENCE. A CR
	not followed by LF is not the delimiter, so `HTTP\r1.1` must be
	ACCEPTED -- and it sits where the scan reaches, which the first
	fixture I tried did not: a trailing CR is excluded by the loop bound
	either way, so it tested the bound and not the comparison. With the
	needle cut to its first byte this case is refused.
	"""
	parsed, resolved = _parts(ROOT / "example" / "http" / "http.situ")
	files = dict(build.generate(parsed, resolved, "http", "situ",
	                            _header(parsed, resolved, "http")))
	files.update(generate_c(parsed, resolved, "http").files())
	for name, text in files.items():
		(tmp_path / name).write_text(text, encoding="utf-8")

	(tmp_path / "probe.c").write_text("""
#include <stdio.h>
#include "http_build.h"

int main(void)
{
	uint8_t     line[64], head[256];
	uint32_t    ln = 0, hn = 0, i;
	situ_err_t  err;
	situ_msg_t  msg;
	situ_view_t view;
	const uint8_t field[] = "Host: example.invalid\\r\\n";

	if (situ_request_line_build(line, sizeof line,
	                            (const uint8_t *)"GET", 3,
	                            (const uint8_t *)"/", 1,
	                            (const uint8_t *)"HTTP/1.1", 8, &ln)
	    != SITU_OK) return 2;

	err = situ_request_head_build(head, sizeof head, line, ln,
	                              field, sizeof field - 1, &hn);
	printf("%d %u ", (int)err, hn);
	for (i = 0; i < hn; i++)
		putchar(head[i] == '\\r' ? '|' : head[i] == '\\n' ? '!' : head[i]);
	printf("\\n");
	if (err != SITU_OK) return 3;

	situ_msg_init(&msg, head, hn);
	printf("%d\\n", (int)situ_request_head_view(&msg, 0, hn, &view));

	/* a version carrying the delimiter: refused, not truncated */
	printf("%d\\n", (int)situ_request_line_build(line, sizeof line,
	        (const uint8_t *)"GET", 3, (const uint8_t *)"/", 1,
	        (const uint8_t *)"HTTP/1.1\\r\\nX", 11, &ln));

	/* a CR not followed by LF is not the delimiter */
	printf("%d\\n", (int)situ_request_line_build(line, sizeof line,
	        (const uint8_t *)"GET", 3, (const uint8_t *)"/", 1,
	        (const uint8_t *)"HTTP\\r1.1", 8, &ln));
	return 0;
}
""", encoding="ascii")

	assert HOST_CC is not None
	done = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "http.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "probe")],
		capture_output=True, text=True)
	assert done.returncode == 0, done.stderr

	ran = subprocess.run([str(tmp_path / "probe")], capture_output=True,
	                     text=True, cwd=tmp_path, timeout=60)
	assert ran.returncode == 0, f"exited {ran.returncode}: {ran.stderr}"
	lines = ran.stdout.split("\n")

	assert lines[0] == "0 41 GET / HTTP/1.1|!Host: example.invalid|!|!", \
		lines[0]
	assert lines[1] == "0", f"the head does not acquire a view: {lines[1]}"
	assert lines[2] == "2", \
		f"a member carrying the delimiter must be refused: {lines[2]}"
	assert lines[3] == "0", (
		"a CR not followed by LF is not the delimiter, so this must be "
		f"accepted -- cut the needle to one byte and it is not: {lines[3]}")
