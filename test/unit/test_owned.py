"""`situc build --target c --owned` (26.69).

The owned form exists for callers that hold a decoded value after the buffer
is gone, so the claim it makes is narrow and checkable: **decode then encode
returns the bytes you started with**. Anything that gets a field's offset,
width, byte order or sign wrong breaks that, and breaks it visibly.

**And that claim is not sufficient, which this file learned the expensive
way.** A round trip catches a conversion applied on one side only. It cannot
catch one dropped from *both* sides: BCD was absent from `_read` and from
`_write` together, so decode-then-encode was byte-identical, `decode`
returned SITU_OK, and the struct held 8215-9-40 for bytes the view read as
2017-9-28. Self-consistent and wrong together. The test that catches it
compares the owned value against the view accessor over the same bytes --
the relationship rather than either side -- and it is below.

Run against every example that has an ownable struct rather than a chosen
one. Which schemas those are is not something anybody picked -- it is
whatever the fixed-size rule admits -- so the coverage moves with the tree
instead of with this file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT, SCHEMAS, ids
from situc.codegen.c import generate as generate_c
from situc.codegen.c import owned
from situc.codegen.c.names import ident, macro
from situc.codegen.c.vectors import parse_vectors
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")

#: What `make test-c` builds generated code with. The owned source is held to
#: the same bar: it is code somebody ships, not a demo.
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion")


def build(path: Path, tmp: Path) -> tuple[list[str], Path]:
	"""Emit the ordinary and owned C for one schema. Returns the ownable
	struct names and where the files went."""
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	emitted = generate_c(schema, resolved, path.stem)
	for name, text in emitted.files().items():
		(tmp / name).write_text(text, encoding="ascii")

	files = owned.generate(schema, resolved, path.stem)
	for name, text in files.items():
		(tmp / name).write_text(text, encoding="ascii")

	return [s.name for s in owned.owned_structs(resolved)], tmp


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_owned_code_compiles_under_the_same_warnings(
		path: Path, tmp_path: Path) -> None:
	"""Generated code that needs a relaxed warning set is generated code
	nobody can put in a build."""
	if COMPILER is None:
		pytest.skip("no C compiler")

	names, where = build(path, tmp_path)
	if not names:
		pytest.skip(f"{path.stem} has no fixed-size struct to own")

	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{where}", f"-I{RUNTIME}",
		 "-c", str(where / f"{path.stem}_owned.c"), "-o", str(where / "o.o")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_decode_then_encode_returns_the_same_bytes(
		path: Path, tmp_path: Path) -> None:
	"""The whole claim of the owned form, over pseudo-random bytes.

	A buffer that fails `validate` is not a round-trip failure -- the decoder
	is right to refuse it -- so those are counted and skipped, and the test
	requires that something was actually round-tripped. A run where every
	draw was refused would otherwise pass while proving nothing, which is the
	shape of half the defects in section 26.
	"""
	if COMPILER is None:
		pytest.skip("no C compiler")

	names, where = build(path, tmp_path)
	if not names:
		pytest.skip(f"{path.stem} has no fixed-size struct to own")

	driver = _driver(path.stem, names, _vectors(path))
	(where / "rt.c").write_text(driver, encoding="ascii")

	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{where}", f"-I{RUNTIME}",
		 str(where / "rt.c"), str(where / f"{path.stem}.c"),
		 str(where / f"{path.stem}_owned.c"), str(RUNTIME / "situ.c"),
		 "-o", str(where / "rt")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(where / "rt")], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr

	# Every draw refused is not a failure and not a pass. A schema with a
	# `must_eq` magic and no committed vectors cannot be reached by random
	# bytes -- the decoder is right to refuse them -- so say so and skip,
	# rather than let a green result stand for a property nothing tested.
	# `test_the_mode_covers_something` is what stops this becoming universal.
	if "round-tripped=0" in ran.stdout:
		pytest.skip(f"{path.stem}: no draw validated ({ran.stdout.strip()}); "
		            f"its constraints need committed vectors to exercise")


def _vectors(path: Path) -> dict[str, list[bytes]]:
	"""The committed corpus for this schema, by struct name.

	Random bytes cannot exercise a schema whose fields carry `must_eq`: an
	ARP packet with a random protocol type is refused, correctly, and 64
	random draws are 64 refusals. The vectors are bytes that do validate --
	laid out by other implementations, which is why they exist -- so where
	they are present they are the better input.
	"""
	corpus = path.with_suffix(".vectors")
	if not corpus.exists():
		return {}

	found: dict[str, list[bytes]] = {}
	for case in parse_vectors(Source(str(corpus),
	                                 corpus.read_text(encoding="ascii"))):
		found.setdefault(case.struct, []).append(case.data)
	return found


def _driver(stem: str, names: list[str],
		corpus: dict[str, list[bytes]]) -> str:
	"""A C main that decodes and re-encodes buffers, then compares.

	Committed vectors where the schema has them, pseudo-random bytes
	otherwise. The C names come from the generator's own helpers rather than
	from uppercasing here -- `wire::framed_body` is a struct in `edges.situ`
	and no amount of `.upper()` produces its macro.
	"""
	lines = [
		"#include <stdio.h>", "#include <string.h>",
		f'#include "{stem}_owned.h"', "",
		"int main(void)", "{",
		"\tunsigned round = 0, refused = 0;",
		"\tuint32_t seed = 20260805u;",
		"",
		"\t(void)seed;   /* unused where every struct has committed vectors */",
		"",
	]

	for name in names:
		held  = ident("situ", name)
		size  = macro("situ", name, "SIZE_FIXED")
		given = corpus.get(name, [])

		if given:
			# One draw per committed vector, copied in rather than generated.
			table = ", ".join(
				"{" + ", ".join(f"0x{byte:02x}" for byte in one) + "}"
				for one in given)
			lines.extend([
				"\t{",
				f"\t\tstatic const uint8_t given[][{len(given[0])}u] = "
				f"{{{table}}};",
				f"\t\tuint8_t wire[{size}], again[{size}];",
				f"\t\t{held}_t value;",
				"",
				f"\t\tfor (unsigned draw = 0; draw < {len(given)}u; ++draw) {{",
				f"\t\t\tmemcpy(wire, given[draw], sizeof wire);",
				"",
			])
		else:
			lines.extend([
				"\t{",
				f"\t\tuint8_t wire[{size}], again[{size}];",
				f"\t\t{held}_t value;",
				"",
				"\t\tfor (unsigned draw = 0; draw < 64u; ++draw) {",
				f"\t\t\tfor (uint32_t i = 0; i < {size}; ++i) {{",
				"\t\t\t\tseed = seed * 1664525u + 1013904223u;",
				"\t\t\t\twire[i] = (uint8_t)(seed >> 24);",
				"\t\t\t}",
				"",
			])

		# The shared tail: decode, re-encode, compare. Identical whether the
		# bytes came from a vector or from the generator above.
		lines.extend([
			f"\t\t\tif ({held}_decode(wire, sizeof wire, &value) != SITU_OK) {{",
			"\t\t\t\t++refused;",
			"\t\t\t\tcontinue;",
			"\t\t\t}",
			"\t\t\tmemset(again, 0xA5, sizeof again);",
			f"\t\t\tif ({held}_encode(&value, again, sizeof again) != SITU_OK) {{",
			f'\t\t\t\tprintf("%s: encode refused\\n", "{name}");',
			"\t\t\t\treturn 1;",
			"\t\t\t}",
			"\t\t\tif (memcmp(wire, again, sizeof wire) != 0) {",
			f'\t\t\t\tprintf("%s: round-trip differs at draw %u\\n", "{name}",'
			" draw);",
			"\t\t\t\treturn 1;",
			"\t\t\t}",
			"\t\t\t++round;",
			"\t\t}",
			"\t}",
			"",
		])

	lines.extend([
		'\tprintf("round-tripped=%u refused=%u\\n", round, refused);',
		"\treturn 0;",
		"}",
		"",
	])
	return "\n".join(lines)


def test_the_mode_covers_something() -> None:
	"""A floor under the skips above.

	Each of those skips is individually correct and collectively they could
	hide the mode covering nothing at all -- a schema stops being ownable,
	a vector file is renamed, and the suite still reports green. This counts
	what is actually exercisable without compiling anything.
	"""
	ownable = 0
	for path in SCHEMAS:
		source   = Source(str(path), path.read_text(encoding="ascii"))
		schema   = parse(source)
		resolved = resolve(schema, solve(schema))
		if owned.owned_structs(resolved):
			ownable += 1

	assert ownable >= 15, (
		f"only {ownable} schemas have an ownable struct; the owned mode has "
		f"quietly stopped covering the tree")


def test_a_variable_length_struct_is_refused_with_a_reason() -> None:
	"""And the refusal names the struct.

	A caller who asked for this mode and found their struct missing from the
	header would conclude the generator was broken. `arp_generic` is the
	standing example: same protocol, same file, and its lengths come from the
	data.
	"""
	path     = ROOT / "example" / "arp" / "arp.situ"
	source   = Source(str(path), path.read_text(encoding="ascii"))
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	refused = dict(owned.refusals(resolved))

	assert "arp_generic" in refused
	assert "decided by the data" in refused["arp_generic"]
	assert "arp_packet" not in refused


def test_the_owned_value_is_the_view_s_value(tmp_path: Path) -> None:
	"""The round trip cannot see a conversion dropped from both sides.

	BCD was absent from the owned form's load *and* its store, so
	decode-then-encode returned the input unchanged, `decode` reported
	SITU_OK, and the struct held a different date from the one the bytes
	spell. `example/rtc` is the schema in the tree with the property, and it
	carries the bit-packed case as well as the byte-aligned one.

	Asserted against the view accessor rather than against a literal, for the
	reason `evidence.md` gives about pinning a relationship: the two are
	generated from one schema and are supposed to agree, so a case where they
	would disagree is the whole test. A literal would go stale the next time
	the corpus moved and would say nothing about the pair.

    Measured before the fix, on `20 17 09 28`:

        view   2017-9-28
        owned  8215-9-40
        round  20 17 09 28  identical

    with the month agreeing in both -- the same coincidence that let the
    original defect reach a real card.
	"""
	if COMPILER is None:
		pytest.skip("no C compiler")

	path         = ROOT / "example" / "rtc" / "rtc.situ"
	names, where = build(path, tmp_path)
	assert "wall_clock" in names, names

	driver = """#include <stdio.h>
#include "rtc.h"
#include "rtc_owned.h"

int main(void)
{
	/* A valid BCD wall clock: 2026-09-06, Sunday, 12:34:56. */
	uint8_t buf[7] = { 0x56u, 0x34u, 0x12u, 0x07u, 0x06u, 0x09u, 0x26u };
	situ_msg_t msg;
	situ_view_t view;
	situ_wall_clock_t owned;
	int bad = 0;

	situ_msg_init(&msg, buf, sizeof buf);
	if (situ_wall_clock_view(&msg, 0u, &view) != SITU_OK) {
		printf("no view\\n");
		return 1;
	}
	if (situ_wall_clock_validate(view) != SITU_OK) {
		printf("fixture does not validate\\n");
		return 1;
	}
	if (situ_wall_clock_decode(buf, sizeof buf, &owned) != SITU_OK) {
		printf("decode refused\\n");
		return 1;
	}

#define SAME(field) do { \\
	unsigned v = (unsigned)situ_wall_clock_##field##_get(view); \\
	unsigned o = (unsigned)owned.field; \\
	printf("%-9s view=%u owned=%u %s\\n", #field, v, o, \\
	       v == o ? "agree" : "DIFFER"); \\
	if (v != o) { bad = 1; } \\
} while (0)

	SAME(seconds);
	SAME(minutes);
	SAME(hours);
	SAME(weekday);
	SAME(day);
	SAME(month);
	SAME(year);
#undef SAME

	return bad;
}
"""
	(where / "vv.c").write_text(driver, encoding="ascii")

	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{where}", f"-I{RUNTIME}",
		 str(where / "vv.c"), str(where / "rtc.c"),
		 str(where / "rtc_owned.c"), str(RUNTIME / "situ.c"),
		 "-o", str(where / "vv")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(where / "vv")], capture_output=True, text=True)
	# The fixture has to reach the comparison, not merely fail to disagree:
	# a driver that refused the view would exit 1 and a driver whose macro
	# never ran would exit 0 having compared nothing.
	assert "DIFFER" not in ran.stdout, ran.stdout
	assert ran.stdout.count("agree") == 7, ran.stdout
	assert ran.returncode == 0, ran.stdout + ran.stderr
