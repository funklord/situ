"""What the cross-backend differential declines, and what it says about it.

`situc/codegen/differ.py` used to refuse a whole schema carrying a
`parameter`, which is why `test/schema/edges.situ` could not grow one: the
corpus exists so that every construct has a gate that can fail on it, and a
construct added there today would have turned four passing sweeps red rather
than filled a gap (26.402).

It skips the struct instead. The tests here are about the skip and nothing
else -- that the ordinary structs are still compared, that every driver says
which struct it left out, and that a skip which would empty the harness is a
refusal rather than a shrink. That last one is the whole reason the filter is
written the way it is: `c/fuzz.py` filtered `example/protobuf` out entirely
and left a harness that compiled, ran, and exercised nothing, at 16 million
executions and coverage 1.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from fourway import COMPLETE
from situc import ast
from situc.codegen import differ
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve

#: Enough of a schema to solve. Byte order and bit order are declared because
#: a schema without them is refused before any of this is reachable.
HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n"

#: One struct the harness compares and one it cannot: `framed` takes an
#: argument the caller supplies, and the driver's whole input is a hex string
#: on argv. `[stream]` because a per-message argument may not move a member
#: (0050), and `body[n]` is what makes this struct worth skipping rather than
#: merely unusual -- it is the shape whose accessors would otherwise read the
#: buffer at the parameter's offset, which is where `body` begins.
MIXED = HEAD + (
	"struct plain { u8 a; u16 b; }\n"
	"struct framed { parameter u8 n [stream]; u8 body[n]; u8 tail; }\n")

#: The same struct with nothing beside it, so the skip takes the last thing
#: the harness had.
ONLY_PARAMETERISED = HEAD + (
	"struct framed { parameter u8 n [stream]; u8 body[n]; u8 tail; }\n")

#: What each backend's driver spells the note as. Listed rather than derived
#: from the generator, which would be the generator agreeing with itself:
#: what is asserted is that the note reaches the OUTPUT in each language, and
#: a renderer that learned the filter and forgot the note fails here.
NOTE = "-- skipped framed: takes a parameter (0050)"

SAID = {
	"c":      f'\tprintf("{NOTE}\\n");',
	"cpp":    f'\tstd::printf("{NOTE}\\n");',
	"rust":   f'\tprintln!("{NOTE}");',
	"python": f'print("{NOTE}")',
}

TARGETS = sorted(SAID)


def build(text: str) -> ResolvedSchema:
	schema = parse_text(text)
	return resolve(schema, solve(schema))


def analysed(text: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse_text(text)
	return schema, resolve(schema, solve(schema))


def test_the_ordinary_struct_is_kept_and_the_parameterised_one_is_not(
		) -> None:
	"""The partition, read off the three functions that make it.

	Asserted as a partition rather than as two memberships: `_acquirable`
	is the population, and a struct that fell out of BOTH halves would be
	a struct the harness lost with nobody naming it.
	"""
	resolved = build(MIXED)

	acquirable = [struct.name for struct in differ._acquirable(resolved)]
	kept       = [struct.name for struct in differ.structs_of(resolved)]
	skipped    = [struct.name for struct in differ.skipped_of(resolved)]

	# The two halves account for the population exactly. Asserted rather
	# than the two memberships alone, because a struct that fell out of
	# BOTH is one the harness lost with nobody naming it -- which is the
	# failure this whole shape is written against.
	assert sorted(kept + skipped) == sorted(acquirable)
	assert kept == ["plain"]
	assert skipped == ["framed"]
	# And each half keeps the containment order it was drawn in, which is
	# what puts the note and the sections in one order in all four drivers.
	assert [name for name in acquirable if name in kept] == kept
	assert [name for name in acquirable if name in skipped] == skipped


@pytest.mark.parametrize("target", TARGETS)
def test_the_harness_compares_the_ordinary_struct(target: str) -> None:
	"""The skip removes one struct and leaves the rest of the harness.

	`-- plain` is the section header every driver prints, so its presence
	is the harness still doing its job. A test that only asserted the
	absence of `framed` would pass just as loudly over a harness the skip
	had emptied.
	"""
	schema, resolved = analysed(MIXED)
	text = differ.generate(schema, resolved, target)

	assert "-- plain" in text
	assert "validate" in text


@pytest.mark.parametrize("target", TARGETS)
def test_every_driver_says_which_struct_it_skipped(target: str) -> None:
	"""A silent absence is a harness that goes green having compared less.

	The note names the struct and the record, in the language of the
	driver that prints it. Each spelling is written out above rather than
	asked of the generator, so a renderer that stopped emitting it fails
	here instead of agreeing with itself.
	"""
	schema, resolved = analysed(MIXED)
	text = differ.generate(schema, resolved, target)

	assert SAID[target] in text


@pytest.mark.parametrize("target", TARGETS)
def test_the_skipped_struct_is_named_nowhere_else(target: str) -> None:
	"""No section, no probe, no write pass -- one line saying it is gone.

	The half that catches a skip applied to the section loop and not to
	the write loop, which reads the same list and is written separately in
	each of the four languages.
	"""
	schema, resolved = analysed(MIXED)
	text = differ.generate(schema, resolved, target)

	rest = text.replace(SAID[target], "")
	assert "framed" not in rest


@pytest.mark.parametrize("target", TARGETS)
def test_a_schema_with_no_parameter_says_nothing(target: str) -> None:
	"""The other direction, which is what makes the note a signal.

	A note printed unconditionally would satisfy every assertion above and
	mean nothing. 41 corpus schemas declare no `parameter`, so this is
	also the shape every one of them has.
	"""
	schema, resolved = analysed(HEAD + "struct plain { u8 a; u16 b; }\n")
	text = differ.generate(schema, resolved, target)

	assert "skipped" not in text
	assert "-- plain" in text


def test_a_struct_that_was_never_acquirable_is_not_called_skipped() -> None:
	"""The note names what the harness LOST, not everything absent from it.

	A struct of nothing but a parameter is zero bytes wide, and a
	zero-length struct is every buffer at once -- so it was held out of
	the harness before the argument was ever asked about, exactly as a
	register is. Calling that a skip would put a line in the output about
	a struct no version of this harness has carried.
	"""
	resolved = build(HEAD + "struct z { parameter u8 n [stream]; }\n"
	                        "struct plain { u8 a; }\n")

	assert resolved.structs["z"].layout.size_bytes == 0
	assert [struct.name for struct in differ._acquirable(resolved)] == ["plain"]
	assert differ.skipped_of(resolved) == []
	assert differ._skip_notes(resolved) == []


def test_a_schema_with_no_structs_is_still_empty_rather_than_refused(
		) -> None:
	"""`std/codecs.situ` declares signatures and no structs at all, and
	`fourway.build` returns an empty command map for it. The refusal below
	must not fire on that: an empty population is not a population that was
	emptied."""
	resolved = build(HEAD)

	assert differ.structs_of(resolved) == []
	assert differ.skipped_of(resolved) == []


def test_a_skip_that_would_empty_the_harness_is_refused() -> None:
	"""The guard this whole shape exists for.

	A harness with every struct skipped compiles, runs, and compares
	nothing, and a sweep reports that as a pass -- which is what
	`c/fuzz.py`'s filter comment records `example/protobuf` costing. So
	the skip fails rather than shrinks.

	The message is held to naming the member and the record, because the
	reader who meets it has just been stopped and is looking for what to
	do next.
	"""
	schema, resolved = analysed(ONLY_PARAMETERISED)

	with pytest.raises(SituError) as refused:
		differ.generate(schema, resolved, "c")

	said = str(refused.value)
	assert "parameter n" in said
	assert "framed" in said
	assert any("0050" in note for note in refused.value.diagnostic.notes)
	assert any("26.402" in note for note in refused.value.diagnostic.notes)


def test_the_refusal_comes_from_structs_of_and_not_from_generate() -> None:
	"""Where the guard lives is the point rather than an implementation
	detail, so it is asserted rather than left to `generate` to carry.

	`fourway.build` asks `differ.structs_of` BEFORE it generates anything
	and returns an empty command map when the answer is empty -- that being
	how a schema with nothing to acquire is passed over. A guard in
	`generate` alone would never run for a schema whose every struct was
	skipped: the sweep would read the empty list, report the schema swept,
	and never call the generator at all.
	"""
	resolved = build(ONLY_PARAMETERISED)

	with pytest.raises(SituError):
		differ.structs_of(resolved)


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_the_harness_with_a_skip_still_builds_and_runs(tmp_path: Path
		) -> None:
	"""Four compilers, four drivers, one buffer, and the same answer.

	The skip is emitted into generated C, C++, Rust and Python, so the
	thing to establish is that each still compiles -- a printf with a
	stray escape or a Rust `println!` with a brace in the struct name
	fails here and nowhere else. The outputs are compared to each other,
	which is what this harness is for, and every one of them carries the
	note.
	"""
	schema = tmp_path / "unit.situ"
	schema.write_text(MIXED, encoding="ascii")

	command = fourway.build(tmp_path, schema)
	assert sorted(command) == TARGETS

	said = {target: fourway.answers(argv, bytes.fromhex("0102030405"),
	                                tmp_path)
	        for target, argv in command.items()}

	for target, out in said.items():
		assert NOTE in out, f"{target} did not say what it skipped"
		assert "-- plain" in out, f"{target} compared nothing"

	assert len(set(said.values())) == 1, said
