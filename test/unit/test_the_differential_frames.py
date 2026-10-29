"""The corpus differential asks `required`, of the structs all four frame.

26.725 measured that `situc/codegen/differ.py` mentioned neither `required`
nor `framed`: the differential every corpus message and every C++ fuzz input
goes through never reached the framing layer, which has more decision
records against it than any other function here. Each driver now prints a
`required()` line per framed struct, one answer per prefix of its input,
and the corpus differential compares the four.

**The four drivers have to call the same set**, or one of them names a
function its backend never emitted and does not compile -- which reads as a
build failure rather than as the disagreement it is. `differ.framed` asks
`frame.framed_structs`, the frame layer's own authority, and this holds
each backend's artifact to it: the bytes-only `required` each one emits, for
the structs the differ compares, is exactly that set. Read back out of the
generated text rather than re-derived, because the opinion that matters is
each emitter's.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

import fourway						# noqa: E402

from situc.codegen import differ			# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.c.names import c_name		# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.codegen.rust.emit import _pascal as pascal	# noqa: E402
from situc.diagnostics import SituError		# noqa: E402

from every_schema import SCHEMAS			# noqa: E402
from test_four_backends_frame_alike import (C_SIGNATURE, analysed,	# noqa: E402
                                            cpp_classes, ids)

RUST_IMPL = re.compile(r"impl(?:<'a>)? (\w+)(?:<'a>)? \{")
RUST_REQUIRED = "pub fn required(data: &[u8]) -> situ_rt::Framing"
PY_CLASS = re.compile(r"class (\w+)\(")
#: One argument after `cls`: a `[stream]` struct takes a second (0050).
PY_REQUIRED = re.compile(r"\tdef required\(cls, data:[^,]*\) ->")

#: schema -> how many structs its drivers frame, filled in as it runs.
FRAMED: dict[str, int] = {}


def _members_of(text: str, opens: re.Pattern[str], marker: str) -> set[str]:
	"""The names of the blocks `opens` starts that contain `marker`."""
	found: set[str] = set()
	current: str | None = None
	for line in text.splitlines():
		head = opens.match(line)
		if head is not None:
			current = head.group(1)
		if current is not None and re.search(marker, line):
			found.add(current)
	return found


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_drivers_frame_what_all_four_emit(schema: Path) -> None:
	parsed, resolved = analysed(schema)
	try:
		compared = {struct.name for struct in differ.structs_of(resolved)}
	except SituError:
		compared = set()		# every acquirable struct takes a parameter
	if not compared:
		pytest.skip(f"{schema.name} has no struct the differ compares")
	asked = differ.framed(resolved)

	c_side = set(C_SIGNATURE.findall(generate_c(parsed, resolved,
	                                            "unit").header))
	# A class whose name collides is emitted as `framed_` with `using
	# framed = framed_;` beside it, so the driver's spelling resolves and
	# the class's own does not match it.
	cpp_side = {name.removesuffix("_")
	            for name in cpp_classes(generate_cpp(parsed, resolved,
	                                                 "unit").header)}
	rust_side = _members_of(generate_rs(parsed, resolved, "unit").module,
	                        RUST_IMPL, re.escape(RUST_REQUIRED))
	py_side = _members_of(generate_py(parsed, resolved, "unit").module,
	                      PY_CLASS, PY_REQUIRED.pattern)

	emitted = {
		"C":      {name for name in compared if c_name(name) in c_side},
		"C++":    {name for name in compared if c_name(name) in cpp_side},
		"Rust":   {name for name in compared if pascal(name) in rust_side},
		"Python": {name for name in compared if c_name(name) in py_side},
	}
	for backend, names in emitted.items():
		assert names == asked, (
			f"{schema.name}: {backend} emits a bytes-only `required` for "
			f"{sorted(names - asked)} beyond what the drivers ask, and not "
			f"for {sorted(asked - names)} that they do")

	for target in ("c", "cpp", "rust", "python"):
		driver = differ.generate(parsed, resolved, target)
		assert driver.count("required()") == len(asked), (
			f"{schema.name}: the {target} driver prints "
			f"{driver.count('required()')} framing lines for {len(asked)} "
			"framed structs")
	FRAMED[schema.name] = len(asked)


def test_the_drivers_framed_something() -> None:
	"""The non-vacuity companion: every assertion above is over a set, and
	an empty set satisfies all of them.

	Measured when written: 222 structs over the corpus.
	"""
	if not FRAMED:
		pytest.skip("the per-schema checks did not run")
	assert sum(FRAMED.values()) >= 200, (
		f"only {sum(FRAMED.values())} structs are framed by the drivers")


#: The two faults the probe found on its first corpus run, as the exact
#: answers rather than as agreement: four backends can agree wrongly.
LABEL_THEN_RUN = """target buffer;
endian big;
struct rec { u16 a; u8 b; }
struct s {
	u8   n;
	u8   label[] until "\\0" max 8;
	rec  run[n];
	u16  tail;
}
"""

WIDE_COUNT = """target buffer;
endian little;
struct w {
	u32  count;
	u16  data[count];
}
"""


def _section_line(output: str, struct: str) -> str:
	"""The `required()` line of one struct's section, not the first one:
	an element struct is framed as well, and its section comes first."""
	where = ""
	for line in output.splitlines():
		if line.startswith("-- "):
			where = line[3:]
		elif where == struct and line.startswith("required()"):
			return line
	raise AssertionError(f"no `required()` line for {struct}:\n{output}")


def _required_line(tmp_path: Path, text: str, struct: str,
		packet: bytes) -> str:
	schema = tmp_path / "unit.situ"
	schema.write_text(text, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.skip(f"a backend could not be built here: {failed}")
	given = {name: _section_line(fourway.answers(argv, packet, tmp_path),
	                             struct)
	         for name, argv in command.items()}
	first = next(iter(given.values()))
	assert all(line == first for line in given.values()), given
	return first


def test_a_delimiter_is_scanned_from_where_its_member_starts(
		tmp_path: Path) -> None:
	"""C scanned from `at`, the running total, rather than the cursor.

	`at` already holds `tail`'s two bytes, so the scan for the label's
	terminator began two bytes past where the label does. Here the label
	is `41 00` and the record after it opens with a zero: scanning from
	there, C measured the label as one byte where it is two and called the
	eight-byte message complete at seven -- a caller then parses a byte
	that has not arrived. Measured with the fix reverted.
	"""
	# n=1, label "A\\0", then one rec 01 02 03, tail 00 00 -- and the rec's
	# own zero bytes are what a scan from the wrong place finds.
	packet = bytes([1, 0x41, 0x00, 0x00, 0x00, 0x03, 0x00, 0x00])
	line = _required_line(tmp_path, LABEL_THEN_RUN, "s", packet)
	assert line.split()[-1] == "*8", line


def test_a_count_times_a_width_saturates(tmp_path: Path) -> None:
	"""C and C++ multiplied a hostile count by the element width in 32
	bits: 0xCCCCCCCC two-byte elements wrapped to 0x99999998, a need a
	caller can meet, where Rust and Python answer the `u32` maximum."""
	packet = bytes([0xCC, 0xCC, 0xCC, 0xCC, 0x01, 0x02])
	line = _required_line(tmp_path, WIDE_COUNT, "w", packet)
	assert line.split()[-1] == "4294967295", line


ARITHMETIC_COUNT = """target buffer;
endian little;
struct rec { u16 x; u8 y; }
struct m {
	u32  a;
	rec  run[a + 1];
	u8   tail;
}
"""


def test_a_run_counted_by_arithmetic_is_framed_as_many(tmp_path: Path) -> None:
	"""`traverse.nests_one_struct` named an expression as a way to spell
	MANY and tested only a literal and a field, so `rec run[a + 1]` was
	framed as ONE `rec` in all four backends: two records, eleven bytes,
	called complete at eight (26.744). All four agreed, which is why the
	differential alone could not see it -- this asserts the number."""
	packet = bytes([1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 9])
	line = _required_line(tmp_path, ARITHMETIC_COUNT, "m", packet)
	assert line.split()[1:] == ["5"] * 4 + ["11"] * 7 + ["*11"], line
