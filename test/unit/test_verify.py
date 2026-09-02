"""`situc verify`: the schema as a specification (26.67).

The command exists for a project that cannot take situ's usual bargain --
where the callers hold owned structs rather than views, or the build may not
gain a code generator -- and can still take a smaller one: keep the
hand-written codec, and let CI fail when the bytes and the schema disagree.

What is worth testing is therefore not that it accepts good vectors. It is
that it *rejects* bad ones, for the right reason and with a line number, and
that it writes nothing while doing so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import main
from situc.codegen.c.vectors import EXPECTATION

ARP     = ROOT / "example" / "arp" / "arp.situ"
VECTORS = ROOT / "example" / "arp" / "arp.vectors"


def test_committed_vectors_conform(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["verify", str(ARP), str(VECTORS)]) == 0
	assert "vectors conform" in capsys.readouterr().out


def test_a_wrong_byte_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The case the whole command is for.

	`wire --check` and `map --check` hold a schema to its own committed
	contracts and never read a real byte, so neither notices a schema that
	disagreed with the implementation from the day it was written. One byte
	of the protocol type is changed here, which is what that disagreement
	looks like from outside.
	"""
	bad = tmp_path / "bad.vectors"
	bad.write_text(VECTORS.read_text(encoding="ascii").replace(
		"00 01 08 00 06 04 00 01", "00 01 09 00 06 04 00 01", 1),
		encoding="ascii")

	assert main(["verify", str(ARP), str(bad)]) == 1

	out = capsys.readouterr().out
	assert "does not conform" in out
	assert "must_eq 2048" in out, "the refusal should name the constraint"
	assert f"{bad}:" in out, "and the line in the vector file"


def test_a_short_vector_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Wrong length and wrong value fail differently, and should read
	differently: one is bytes the layout cannot cover, the other is bytes it
	covers and forbids."""
	short = tmp_path / "short.vectors"
	short.write_text("arp_packet truncated 00 01 08 00\n", encoding="ascii")

	assert main(["verify", str(ARP), str(short)]) == 1
	assert "BoundsError" in capsys.readouterr().out


def test_an_unknown_struct_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	names = tmp_path / "names.vectors"
	names.write_text("no_such_struct case 00 01\n", encoding="ascii")

	assert main(["verify", str(ARP), str(names)]) == 1
	assert "no struct `no_such_struct`" in capsys.readouterr().out


def test_an_empty_corpus_is_not_a_pass(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Nought out of nought conforming is not evidence of anything, and a
	green exit there would be a CI job that passes for having no input --
	which is the failure this whole command is meant to prevent one level
	down."""
	empty = tmp_path / "empty.vectors"
	empty.write_text("# nothing but a comment\n", encoding="ascii")

	assert main(["verify", str(ARP), str(empty)]) == 1
	assert "holds no vectors" in capsys.readouterr().out


def test_it_generates_nothing(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The property that makes it adoptable.

	A project taking this mode has declined to put a code generator in its
	build; a command that quietly wrote a `.py` beside the schema would have
	given it one anyway.
	"""
	work = tmp_path / "work"
	work.mkdir()
	before = set(work.rglob("*"))

	vectors = work / "copy.vectors"
	vectors.write_text(VECTORS.read_text(encoding="ascii"), encoding="ascii")

	assert main(["verify", str(ARP), str(vectors)]) == 0
	capsys.readouterr()

	assert set(work.rglob("*")) == before | {vectors}


def test_every_example_with_vectors_conforms_to_its_schema(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""And the corpora already in the tree are held to their schemas.

	These vectors were laid out by other implementations -- glibc's ARP
	definitions, an archive GNU cpio wrote, bytes a netlink socket handed
	back -- so this is the differential check of `test/unit/oracles.py` in
	the form a checking-only adopter would use it.
	"""
	found = sorted(ROOT.glob("example/*/*.vectors"))
	assert found, "no committed vectors to check"

	failed = []
	for vectors in found:
		schema = vectors.with_suffix(".situ")
		if not schema.exists():
			continue
		if main(["verify", str(schema), str(vectors)]) != 0:
			failed.append(vectors.parent.name)
		capsys.readouterr()

	assert not failed, f"committed vectors do not conform: {failed}"


# --------------------------------------------------------------------------
# The expectations under each case, which were parsed and thrown away.
# --------------------------------------------------------------------------

#: `arp_packet` with the sender's two addresses swapped for the target's.
#: Every byte of every vector is still the right length and still passes
#: `validate` -- the fields simply read each other's bytes, which is exactly
#: the disagreement a corpus exists to catch and the one nothing caught.
SWAPPED = ("""	u8            sender_hardware[6];
	u8            sender_protocol[4];
	u8            target_hardware[6];
	u8            target_protocol[4];""",
           """	u8            target_hardware[6];
	u8            target_protocol[4];
	u8            sender_hardware[6];
	u8            sender_protocol[4];""")


def swapped_schema(tmp_path: Path) -> Path:
	text = ARP.read_text(encoding="ascii")
	assert SWAPPED[0] in text, "the fixture no longer matches example/arp/arp.situ"

	schema = tmp_path / "swapped.situ"
	schema.write_text(text.replace(*SWAPPED, 1), encoding="ascii")
	return schema


def test_an_expectation_that_does_not_hold_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The defect this pass exists for.

	`check` called `parse_vectors`, which fills in `Case.expectations`, and
	then handed only `case.data` to the refusal: the string `expectations`
	did not appear in the module at all. So the fourteen lines under ARP's
	two vectors -- which say what each field must read -- were checked by
	nothing, and a schema whose sender and target addresses read out of each
	other's bytes reported "2 vectors conform" while `situc wire --check`
	called the same edit BREAKING.
	"""
	assert main(["verify", str(swapped_schema(tmp_path)), str(VECTORS)]) == 1

	out = capsys.readouterr().out
	assert "does not conform" in out
	assert "4 of 9 expectations do not hold" in out
	assert ("`sender_hardware` is 00 00 00 00 00 00, and the vector expects "
	        "00:1A:2B:3C:4D:5E") in out, \
		"the field, what was read and what was expected"


def test_a_scalar_expectation_is_compared_as_a_number(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""And the spelling the vector uses is the generated header's.

	`operation = SITU_OPERATION_REPLY` is what `situc gen-tests` drops into
	cmocka, so it is what this has to read -- resolved through the same
	`macro()` the C emitter names enum members with, rather than a second
	spelling invented here.
	"""
	wrong = tmp_path / "wrong.vectors"
	wrong.write_text(VECTORS.read_text(encoding="ascii").replace(
		"operation       = SITU_OPERATION_REQUEST",
		"operation       = SITU_OPERATION_REPLY", 1), encoding="ascii")

	assert main(["verify", str(ARP), str(wrong)]) == 1
	assert "`operation` is 1, and the vector expects 2 (SITU_OPERATION_REPLY)" \
		in capsys.readouterr().out


def test_a_nested_expectation_reads_through_the_inner_view(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""`file.file_size` is `bitmap_file_header`'s field, not `bitmap_file`'s.

	The C generator learned that a nested struct's accessors belong to its
	own type (26.35); the same path here is an attribute reached through the
	sub-view, and asking the outer one would have found nothing to compare.
	"""
	bmp     = ROOT / "example" / "bmp" / "bmp.situ"
	vectors = ROOT / "example" / "bmp" / "bmp.vectors"

	wrong = tmp_path / "nested.vectors"
	wrong.write_text(vectors.read_text(encoding="ascii").replace(
		"file.file_size     = 78", "file.file_size     = 79", 1),
		encoding="ascii")

	assert main(["verify", str(bmp), str(wrong)]) == 1
	assert "`file.file_size` is 78, and the vector expects 79" in \
		capsys.readouterr().out


def test_a_malformed_expectation_says_so(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""A vector file can be wrong about the schema in ways that are not a
	value: a field that does not exist, a byte run given the wrong number of
	bytes, an enum constant nothing defines. `gen-tests` refuses all three
	before it emits C, and this refuses them against the same layout rather
	than reporting a pass it did not check."""
	bad = tmp_path / "malformed.vectors"
	bad.write_text(
		"arp_packet who_has 00 01 08 00 06 04 00 01 00 1A 2B 3C 4D 5E"
		" C0 A8 01 0A 00 00 00 00 00 00 C0 A8 01 01\n"
		"\tno_such_field   = 1\n"
		"\toperation       = SITU_OPERATION_NOPE\n"
		"\tsender_hardware = 00:1A:2B\n", encoding="ascii")

	assert main(["verify", str(ARP), str(bad)]) == 1

	out = capsys.readouterr().out
	assert "`no_such_field` is not a field of `arp_packet`" in out
	assert ("`operation`: `SITU_OPERATION_NOPE` is neither an integer nor an "
	        "enum constant of this schema") in out
	assert "`sender_hardware` is 6 bytes and its expectation is 3" in out


def test_every_committed_expectation_is_load_bearing(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The control on the control.

	That the committed corpora pass is not evidence that their expectations
	are read: they passed while being read by nothing at all, which is how
	the defect survived. So each expectation line is changed, one at a time,
	and the corpus has to fail for it. Four schemas rather than eleven,
	chosen for the four shapes an expectation takes -- ARP's byte runs and
	enum constants, NTP's nested timestamps and hex, cpio's text numbers,
	BMP's nested byte run -- because each mutation rebuilds the accessors.
	"""
	mutated = tmp_path / "mutated.vectors"
	checked = 0

	for name in ("arp", "ntp", "cpio", "bmp"):
		schema  = ROOT / "example" / name / f"{name}.situ"
		vectors = ROOT / "example" / name / f"{name}.vectors"
		lines   = vectors.read_text(encoding="ascii").splitlines()

		for number, raw in enumerate(lines):
			if not raw[:1].isspace():
				continue
			match = EXPECTATION.match(raw)
			assert match is not None, f"{vectors}:{number + 1} is not an expectation"

			changed = list(lines)
			changed[number] = raw[:match.start(2)] + _other(match.group(2))
			mutated.write_text("\n".join(changed) + "\n", encoding="ascii")

			assert main(["verify", str(schema), str(mutated)]) == 1, \
				f"{vectors}:{number + 1} ({raw.strip()}) is checked by nothing"
			capsys.readouterr()
			checked += 1

	assert checked > 60, f"only {checked} expectations were exercised"


def _other(value: str) -> str:
	"""A value the field cannot also be reading.

	`5A` repeated for a byte run, so the count stays right and only the bytes
	are wrong; a number nothing in the corpus holds otherwise.
	"""
	digits = value.replace(":", "").replace(" ", "").replace("-", "")
	if (":" in value or " " in value) and not value.lower().startswith("0x"):
		return " ".join(["5A"] * (len(digits) // 2))
	return "987654"


# --------------------------------------------------------------------------
# "A view over exactly its bytes", which was a lower bound only.
# --------------------------------------------------------------------------

def test_surplus_bytes_are_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""`check`'s docstring said "exactly its bytes" and enforced half of it.

	`at` refuses a frame shorter than the layout and nothing refused a longer
	one, so 228 bytes conformed to a 28-byte fixed struct -- 200 of them read
	by nobody and asserted about by a passing CI job.
	"""
	surplus = tmp_path / "surplus.vectors"
	surplus.write_text(
		"arp_packet padded 00 01 08 00 06 04 00 01 00 1A 2B 3C 4D 5E"
		" C0 A8 01 0A 00 00 00 00 00 00 C0 A8 01 01 " + "FF " * 200 + "\n",
		encoding="ascii")

	assert main(["verify", str(ARP), str(surplus)]) == 1
	assert "the layout covers 28 of the vector's 228 bytes; 200 are surplus" \
		in capsys.readouterr().out


def test_surplus_after_a_variable_struct_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""A variable struct has no constant to compare against, and the extent
	is not therefore unknowable: `required` is the accessors' own answer to
	how far these bytes reach, and a cpio entry says so through its own
	length fields."""
	cpio    = ROOT / "example" / "cpio" / "cpio.situ"
	vectors = ROOT / "example" / "cpio" / "cpio.vectors"

	case = next(line for line in
	            vectors.read_text(encoding="ascii").splitlines()
	            if line.startswith("cpio_entry greeting"))

	surplus = tmp_path / "surplus.vectors"
	surplus.write_text(case + " " + "FF " * 20 + "\n", encoding="ascii")

	assert main(["verify", str(cpio), str(surplus)]) == 1
	assert "the layout covers 132 of the vector's 152 bytes; 20 are surplus" \
		in capsys.readouterr().out


def test_a_located_member_may_reach_past_its_frame(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The control on the upper bound, and the reason it is not the frame.

	`bitmap_file` is a fixed 54 bytes of headers and `example/bmp`'s vector
	is a whole 78-byte file, because `pixels` is placed where the *data* says
	from the start of the message (9.8). A strict `len == SIZE_BYTES` would
	refuse that committed vector, which is the same trap
	`codegen/c/vectors._check` fell into and records.
	"""
	bmp     = ROOT / "example" / "bmp" / "bmp.situ"
	vectors = ROOT / "example" / "bmp" / "bmp.vectors"

	assert main(["verify", str(bmp), str(vectors)]) == 0
	assert "vectors conform" in capsys.readouterr().out


# --------------------------------------------------------------------------
# What the report may claim, given what `validate` does not walk.
# --------------------------------------------------------------------------

ARRAYS = """target buffer;
endian big;

enum k : u8 {
	one = 1,
	two = 2,
}

struct inner {
	u8 tag [must_eq = 9];
}

struct outer {
	k     kinds[2];
	inner many[2];
}
"""


def test_an_unwalked_array_qualifies_the_report(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Neither `07` as a `k` nor `05` where `must_eq = 9` is refused, and
	that is deliberate: an array of structs gets an accessor per element and
	no per-element validation, "because walking every element on every parse
	is a cost the caller should choose" (`traverse.Check`). All four backends
	agree, so this does not change it.

	What it changes is the sentence. A conformance tool that prints an
	unqualified "1 vectors conform" while inheriting a documented gap is
	claiming more than it checked.
	"""
	schema = tmp_path / "arrays.situ"
	schema.write_text(ARRAYS, encoding="ascii")

	vectors = tmp_path / "arrays.vectors"
	vectors.write_text("outer bad 01 07 09 05\n", encoding="ascii")

	assert main(["verify", str(schema), str(vectors)]) == 0, \
		"the report is qualified; the behaviour is not changed"

	out = capsys.readouterr().out
	assert "1 vectors conform" in out
	assert "except inside arrays" in out
	assert "`outer.kinds`: every element is a `k`" in out
	assert "`outer.many`: every `inner` in it carries constraints" in out


def test_a_schema_without_arrays_is_not_qualified(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""And the qualification is about this schema rather than about the
	command: ARP has no array of structs and no enum array, so its report
	says what it always said. A note printed over every corpus would be read
	as boilerplate and stop being read at all."""
	assert main(["verify", str(ARP), str(VECTORS)]) == 0

	out = capsys.readouterr().out
	assert "2 vectors conform" in out
	assert "except inside arrays" not in out
	assert "note:" not in out
