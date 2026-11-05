"""Several variants are one builder, each arm a value (26.881).

A builder per combination is a product of the arms, and every flag-gated
member of a format is a variant: ten of them were 1024 builders and a 9 MB
header. Each arm is one member and the variants choose independently, so
what grows here is their sum.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import fourway						# noqa: E402
from every_schema import ROOT, load_schema		# noqa: E402
from test_the_four_builders_agree import (Given, _combinations,	# noqa: E402
                                          _flat, _label, four_say)

from situc.codegen import plain_build			# noqa: E402
from situc.codegen.c.build import CHOSEN		# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n"\
       "struct nothing { }\n"

#: Every kind of arm, and every way a discriminant is written.
SHAPES = HEAD + """
struct inner { u8 n; u8 data[n]; }
struct packed_tags {
	u1 a;
	u1 b;
	reserved u6 [must_be_zero];
	variant one switch (a) {
		case 1: inner   one_nested;
		case 0: nothing one_absent;
		default: error;
	}
	variant two switch (b) {
		case 1: u16     two_scalar;
		case 0: u8      two_run[3];
		default: error;
	}
	u8 after;
}
struct whole_tags {
	u8 kind;
	u16 mode;
	variant first switch (kind) {
		case 1: u32   first_wide;
		case 2: inner first_nested;
		default: u8   first_other;
	}
	variant second switch (mode) {
		case 7: nothing second_absent;
		default: inner  second_other;
	}
}
"""


def _write(tmp_path: Path, text: str) -> Path:
	schema = tmp_path / "shapes.situ"
	schema.write_text(text, encoding="ascii")
	return schema


def _units(schema: Path) -> list[plain_build.Unit]:
	parsed   = load_schema(schema)
	resolved = resolve(parsed, solve(parsed))
	return plain_build.units(resolved, plain_build.shape(parsed, resolved))


def _flags(count: int) -> str:
	"""`count` optional members, each behind its own flag."""
	lines = [HEAD, "struct msg {"]
	lines.extend(f"\tu1 f{at};" for at in range(count))
	if count % 8:
		lines.append(f"\treserved u{8 - count % 8} [must_be_zero];")
	for at in range(count):
		lines.extend([f"\tvariant v{at} switch (f{at}) {{",
		              f"\t\tcase 1: u32 v{at}_present;",
		              f"\t\tcase 0: nothing v{at}_absent;",
		              "\t\tdefault: error;", "\t}"])
	lines.append("}")
	return "\n".join(lines) + "\n"


def _emitted(schema: Path, target: str, out: Path) -> int:
	"""How many bytes of builder `target` writes."""
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 target, "--layer", "edit", "--out", str(out)],
		capture_output=True, text=True, cwd=ROOT)
	assert done.returncode == 0, done.stderr[-800:]
	return sum(one.stat().st_size for one in out.glob("*_build*"))


@pytest.mark.parametrize("target", ["c", "cpp", "rust", "python"])
def test_what_is_generated_grows_with_the_sum_of_the_arms(
		tmp_path: Path, target: str) -> None:
	"""Doubling the flags about doubles the output. A product squares it,
	which is 16 here, and at 24 flags it would not have finished."""
	sizes = []
	for count in (6, 12, 24):
		(tmp_path / str(count)).mkdir()
		schema = tmp_path / str(count) / "flags.situ"
		schema.write_text(_flags(count), encoding="ascii")
		built = [one for one in _units(schema) if one.struct.name == "msg"]
		assert len(built) == 1 and not built[0].suffix
		sizes.append(_emitted(schema, target, tmp_path / str(count)))
	assert sizes[1] < 2.5 * sizes[0] and sizes[2] < 2.5 * sizes[1], sizes


def test_one_variant_keeps_a_builder_per_arm(tmp_path: Path) -> None:
	"""One variant is no product, and the arm is which builder you call."""
	schema = _write(tmp_path, HEAD + """
		struct one {
			u8 kind;
			variant v switch (kind) {
				case 1: u8  a;
				case 2: u16 b;
				default: error;
			}
		}
	""")
	built = [one for one in _units(schema) if one.struct.name == "one"]
	assert {"a", "b"} <= {one.suffix for one in built}
	assert not any(part.role == CHOSEN for one in built for part in one.parts)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_every_combination_of_every_kind_of_arm_is_written_alike(
		tmp_path: Path) -> None:
	"""All four, on each combination, with each result read back: a nested
	arm, an arm of no bytes, a scalar, a fixed run and a default arm, behind
	packed and whole discriminants."""
	schema = _write(tmp_path, SHAPES)
	units  = _units(schema)
	said   = four_say(schema, tmp_path, units, [])
	made   = {line.rsplit(" ", 1)[0] for line in said.splitlines()
	          if not line.endswith(" refused")}
	wanted = {_label(one) for one in _combinations(units)
	          if one.struct.name in ("packed_tags", "whole_tags")}
	assert len(wanted) == 4 + 6
	assert wanted <= made, sorted(wanted - made)


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_the_flags_say_which_arms_were_handed_over(tmp_path: Path) -> None:
	"""The discriminant comes from the arm. Agreement alone would pass all
	four writing the same wrong flags, so the bytes are stated."""
	schema = _write(tmp_path, SHAPES)
	units  = [one for one in _units(schema)
	          if one.struct.name == "packed_tags"]
	said   = dict(line.rsplit(" ", 1) for line in four_say(
		schema, tmp_path, units, []).splitlines())
	flags  = {label.split(".")[1]: int(made[:2], 16)
	          for label, made in said.items() if made != "refused"}
	assert flags["one_nested+two_scalar"] == 0b11000000
	assert flags["one_nested+two_run"]    == 0b10000000
	assert flags["one_absent+two_scalar"] == 0b01000000
	assert flags["one_absent+two_run"]    == 0b00000000


def test_python_refuses_an_arm_of_another_variant(tmp_path: Path) -> None:
	"""The other three say so at compile time, and Python has only this."""
	schema = _write(tmp_path, SHAPES)
	_emitted(schema, "python", tmp_path)
	(tmp_path / "situ_runtime.py").write_bytes(
		(ROOT / "runtime/python/situ_runtime.py").read_bytes())
	done = subprocess.run([sys.executable, "-c", """
import shapes_build as b
import situ_runtime
wrong = b.packed_tags_two_arm.two_scalar(1)
try:
	b.build_packed_tags(bytearray(64), wrong, wrong, 0)
except situ_runtime.ConstraintError:
	print("refused")
"""], capture_output=True, text=True, cwd=tmp_path)
	assert done.stdout.strip() == "refused", done.stderr[-800:]


#: Default arms over each way a tag is held, and a peek beside a variant.
GUARDED = HEAD + """
struct word { u8 n; u8 data[n]; }
struct ping { u8 kind; u8 value; }
struct other { u8 kind; u8 value; u8 more; }
struct whole {
	u8 kind;
	u16 mode;
	variant first switch (kind) {
		case 1: u32 first_wide;
		default: u8 first_other;
	}
	variant second switch (mode) {
		case 7: nothing second_absent;
		default: word second_other;
	}
}
struct packed {
	u2 x;
	u2 y;
	reserved u4 [must_be_zero];
	variant first switch (x) {
		case 1: u8 first_named;
		default: u8 first_other;
	}
	variant second switch (y) {
		case 1: nothing second_named;
		default: u8 second_other;
	}
}
struct peeking {
	u8 mode;
	peek u8 kind;
	variant first switch (kind) {
		case 1: ping first_ping;
		default: other first_other;
	}
	variant second switch (mode) {
		case 0: nothing second_absent;
		case 1: u8 second_byte;
		default: error;
	}
}
"""


def _guarded(tmp_path: Path, name: str, cases: list[tuple[str, tuple[int | bytes, ...]]]
		) -> list[str]:
	"""What all four say to each (combination, values), in order."""
	schema = _write(tmp_path, GUARDED)
	units  = _units(schema)
	picks  = {_label(one): one for one in _combinations(units)}
	extra: list[tuple[plain_build.Unit, Given]] = []
	for label, values in cases:
		unit  = picks[f"{name}.{label}"]
		taken = [part for part in _flat(unit)
		         if plain_build.takes_value(part)
		         or part.role in plain_build.MEASURED]
		assert len(taken) == len(values), [one.role for one in taken]
		extra.append((unit, list(zip(taken, values))))
	said = four_say(schema, tmp_path, units, extra)
	return [line.rsplit(" ", 1)[1] for line in said.splitlines()[-len(cases):]]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_default_arm_may_not_claim_a_named_arm_s_value(
		tmp_path: Path) -> None:
	"""It would read as that arm: the tag is the caller's, and not freely."""
	assert _guarded(tmp_path, "whole", [
		("first_other+second_other", (9, 5, 3, b"\x01\xaa")),
		("first_other+second_other", (1, 5, 3, b"\x01\xaa")),	# first's
		("first_other+second_other", (9, 5, 7, b"\x01\xaa")),	# second's
		("first_wide+second_absent", (0x01020304,)),
	]) == ["09" "0003" "05" "01aa", "refused", "refused",
	       "01" "0007" "01020304"]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_packed_default_tag_has_to_fit_and_not_be_claimed(
		tmp_path: Path) -> None:
	"""Masking it would write a valid message saying something else."""
	assert _guarded(tmp_path, "packed", [
		("first_other+second_other", (2, 5, 3, 6)),
		("first_other+second_other", (4, 5, 3, 6)),	# wider than `x`
		("first_other+second_other", (5, 5, 3, 6)),	# 1 once masked
		("first_other+second_other", (1, 5, 3, 6)),	# first's
		("first_named+second_named", (9,)),
	]) == [f"{0b10110000:02x}" "05" "06", "refused", "refused", "refused",
	       f"{0b01010000:02x}" "09"]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_a_peeked_arm_still_says_which_it_is(tmp_path: Path) -> None:
	"""A peek writes nothing, so the arm's first byte is what is checked,
	with a variant beside it choosing on its own."""
	assert _guarded(tmp_path, "peeking", [
		("first_ping+second_absent", (b"\x01\x07",)),
		("first_ping+second_absent", (b"\x02\x07",)),	# not a ping
		("first_ping+second_byte", (b"\x01\x07", 3)),
		("first_other+second_absent", (b"\x02\x07\x08",)),
		("first_other+second_absent", (b"\x01\x07\x08",)),	# reads as ping
	]) == ["00" "0107", "refused", "01" "0107" "03", "00" "020708", "refused"]


NAMED = HEAD.replace("{ }", "{{ }}") + """
struct s {{
	u8 a;
	u8 b;
	variant {variant} switch (a) {{
		case 1: u8 {arm};
		case 0: nothing none_a;
		default: error;
	}}
	variant other switch (b) {{
		case 1: u16 two;
		case 0: nothing none_b;
		default: error;
	}}
}}
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
@pytest.mark.parametrize("place", ["variant", "arm"])
@pytest.mark.parametrize("name", ["plain", "self", "len", "isinstance", "type",
                                  "register", "class", "match"])
def test_an_arm_or_a_variant_may_have_any_name(tmp_path: Path, place: str,
		name: str) -> None:
	"""`self` capitalises into a keyword, and `len` took the builtin the
	builder calls. `plain` says the template works."""
	schema = _write(tmp_path, NAMED.format(
		variant=name if place == "variant" else "first",
		arm=name if place == "arm" else "one"))
	units  = [one for one in _units(schema) if one.struct.name == "s"]
	said   = four_say(schema, tmp_path, units, [])
	made   = [line for line in said.splitlines()
	          if not line.endswith(" refused")]
	assert len(made) >= 4, said


def test_a_member_may_take_the_name_of_a_builtin_the_builder_calls(
		tmp_path: Path) -> None:
	"""Not only an arm: a plain run called `len` could not be built."""
	schema = _write(tmp_path, HEAD + "struct s { u8 n; u8 len[n]; }\n")
	_emitted(schema, "python", tmp_path)
	(tmp_path / "situ_runtime.py").write_bytes(
		(ROOT / "runtime/python/situ_runtime.py").read_bytes())
	done = subprocess.run([sys.executable, "-c", """
import shapes_build as b
out = bytearray(8)
print(bytes(out[:b.build_s(out, b"ab")]).hex())
"""], capture_output=True, text=True, cwd=tmp_path)
	assert done.stdout.strip() == "026162", done.stderr[-800:]
