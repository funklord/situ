"""The `.proto` importer (project.md sections 19.2 and 26.13).

Section 26.13 calls this "the one component where a silent partial success does
real harm", and that shapes the tests: as much of this file is about what the
importer *refuses* as about what it translates. An importer that produced a
plausible-looking schema from a `.proto` it only half understood would be worse
than no importer, because the user would trust the output.

The translation itself is checked against `protoc` where it is available. A
schema that agrees only with situ's own reading of protobuf has demonstrated
nothing, which is the same argument the phase 6 conformance gate made.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from situc import proto
from situc.diagnostics import Source
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve

ROOT   = Path(__file__).resolve().parents[2]
PROTOC = shutil.which("protoc")

TRANSLATABLE = """syntax = "proto2";

package example.wire;

message Profile {
	optional uint64 user_id  = 1;
	optional string username = 2;
	optional float  score    = 3;
	optional sint32 delta    = 4;
	optional fixed64 seen_at = 5;
	repeated string tags     = 6;
	optional Status status   = 7;

	oneof contact {
		string email = 8;
		uint64 phone = 9;
	}
}

enum Status {
	UNKNOWN = 0;
	ACTIVE  = 1;
	CLOSED  = 2;
}
"""


def imported(text: str) -> proto.Imported:
	return proto.read(text)


def translated(text: str, source: str = "unit.proto") -> str:
	return proto.translate(proto.read(text), source)


def compiled(text: str) -> object:
	schema = parse(Source("unit.situ", translated(text)))
	return resolve(schema, solve(schema))


# -- what translates (19.2) -------------------------------------------------


def test_a_message_becomes_a_struct_holding_one_tlv_region() -> None:
	"""Which is what a message is on the wire: no positional structure at all."""
	source = translated(TRANSLATABLE)

	assert "struct profile {" in source
	assert "tlv fields (" in source
	assert "tag_decode     = { field = tag >> 3, wire = tag & 0x7 }" in source


def test_field_numbers_and_types_become_known_entries() -> None:
	source = translated(TRANSLATABLE)

	assert "1 : { name = user_id, wire = 0, type = pb_varint }" in source
	assert "2 : { name = username, wire = 2, type = u8 }" in source
	assert "3 : { name = score, wire = 5, type = f32 }" in source
	assert "5 : { name = seen_at, wire = 1, type = u64 }" in source


def test_a_zigzag_field_gets_the_zigzag_varint() -> None:
	"""`sint32` is not `int32`: the transform is part of the encoding."""
	source = translated(TRANSLATABLE)

	assert "4 : { name = delta, wire = 0, type = pb_zigzag }" in source
	assert "transform = zigzag;" in source


def test_a_zigzag_type_is_not_declared_when_nothing_uses_it() -> None:
	source = translated("""syntax = "proto2";
	message M { optional uint64 a = 1; }
	""")
	assert "pb_zigzag" not in source


def test_repeated_becomes_duplicate_tags_allowed() -> None:
	assert "duplicate_tags = allowed" in translated(TRANSLATABLE)


def test_an_enum_becomes_one_with_default_pass_and_says_why() -> None:
	"""Protobuf's semantics, and one of the five reasons it is not canonical."""
	source = translated(TRANSLATABLE)

	assert "enum status : i32 {" in source
	assert "\tACTIVE = 1," in source
	assert "\tdefault = pass," in source
	assert "makes\n// the enclosing struct NonCanonical" in source


def test_a_oneof_is_noted_as_unenforceable() -> None:
	"""On the wire they are ordinary fields; the invariant is semantic."""
	source = translated(TRANSLATABLE)

	assert "`oneof contact` covered fields 8, 9" in source
	assert "a semantic constraint the layout cannot enforce" in source
	# And the fields are still translated, because they are still on the wire.
	assert "8 : { name = email, wire = 2, type = u8 }" in source


def test_the_imported_schema_compiles() -> None:
	"""An importer whose output its own compiler rejects has produced nothing."""
	resolved = compiled(TRANSLATABLE)
	assert resolved.find_struct("profile") is not None		# type: ignore[attr-defined]


def test_the_imported_schema_records_what_the_format_costs() -> None:
	"""The assertions fail on purpose: each is true about protobuf."""
	source = translated(TRANSLATABLE)

	assert "assert canonical(profile);" in source
	assert "assert random_access(profile.fields);" in source
	assert "require absolute_static(profile.fields);" in source


def test_message_names_are_converted_to_the_tree_convention() -> None:
	source = translated("""syntax = "proto2";
	message UserProfile { optional uint64 id = 1; }
	""")
	assert "struct user_profile {" in source


# -- what does not translate, and must be reported --------------------------


def losses_of(text: str) -> dict[str, proto.Loss]:
	return {loss.construct.split("`")[1] if "`" in loss.construct
	        else loss.construct: loss
	        for loss in imported(text).losses}


def test_a_map_is_refused_with_a_remedy() -> None:
	found = losses_of("""syntax = "proto2";
	message M { map<string, int32> counters = 1; }
	""")

	assert "counters" in str(found)
	loss = next(iter(found.values()))
	assert loss.line == 2
	assert "unordered" in loss.reason
	assert "repeated` entries" in loss.remedy


def test_a_reflective_well_known_type_is_refused() -> None:
	found = losses_of("""syntax = "proto2";
	message M { optional google.protobuf.Any payload = 1; }
	""")

	assert "payload" in found
	assert "resolved at run time" in found["payload"].reason


def test_a_group_is_refused_and_says_why_it_cannot_be_skipped() -> None:
	found = losses_of("""syntax = "proto2";
	message M { optional group Legacy = 1 { optional int32 x = 1; } }
	""")

	assert "Legacy" in found
	assert "no length prefix" in found["Legacy"].reason


def test_a_service_is_out_of_scope() -> None:
	found = losses_of("""syntax = "proto2";
	message M { optional int32 a = 1; }
	service Directory { rpc Lookup (M) returns (M); }
	""")

	assert "Directory" in found and "Lookup" in found
	assert "describe behaviour, not bytes" in found["Directory"].reason


def test_proto3_absent_versus_zero_is_reported() -> None:
	"""A semantic property with no layout expression: both encode as no bytes."""
	losses = imported("""syntax = "proto3";
	message M { int32 a = 1; }
	""").losses

	assert len(losses) == 1
	assert "absent scalar from one set to zero" in losses[0].reason


def test_proto2_reports_nothing_of_its_own() -> None:
	assert not imported(TRANSLATABLE).losses


def test_every_loss_carries_a_source_location() -> None:
	"""Section 19.2 asks for the location by name, and a report without one
	sends the reader hunting through a file the importer already read."""
	text = """syntax = "proto2";
	message M {
		map<string, int32> counters = 1;
		optional group G = 2 { optional int32 x = 1; }
	}
	"""
	for loss in imported(text).losses:
		assert loss.line > 0
		assert text.splitlines()[loss.line - 1].strip()


def test_the_report_names_each_construct_and_offers_a_way_out() -> None:
	text = """syntax = "proto2";
	message M { map<string, int32> counters = 1; }
	"""
	rendered = proto.report(imported(text), "m.proto")

	assert "1 construct(s) in m.proto" in rendered
	assert "m.proto:2:" in rendered
	assert "--accept-lossy" in rendered


# -- the command (26.13) ----------------------------------------------------


def run(*args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		["python3", "-m", "situc.cli", *args],
		cwd=ROOT, capture_output=True, text=True)


def test_a_lossy_import_exits_non_zero(tmp_path: Path) -> None:
	source = tmp_path / "m.proto"
	source.write_text('syntax = "proto2";\n'
	                  "message M { map<string, int32> c = 1; }\n", encoding="ascii")

	result = run("import-proto", str(source), "-o", str(tmp_path / "m.situ"))

	assert result.returncode == 1
	assert "map<string, int32>" in result.stderr
	assert not (tmp_path / "m.situ").exists()


def test_accept_lossy_downgrades_to_warnings_and_still_compiles(
	tmp_path: Path,
) -> None:
	"""Section 26.13's third criterion, verbatim."""
	source = tmp_path / "m.proto"
	source.write_text('syntax = "proto2";\n'
	                  "message M {\n"
	                  "\toptional uint64 id = 1;\n"
	                  "\tmap<string, int32> c = 2;\n"
	                  "}\n", encoding="ascii")
	out = tmp_path / "m.situ"

	result = run("import-proto", str(source), "-o", str(out), "--accept-lossy")

	assert result.returncode == 0
	assert "map<string, int32>" in result.stderr		# still reported
	assert out.exists()

	# And the schema it wrote is one situ accepts.
	assert run("map", str(out)).returncode == 0


def test_a_proto_with_no_messages_is_refused(tmp_path: Path) -> None:
	source = tmp_path / "m.proto"
	source.write_text('syntax = "proto2";\npackage a.b;\n', encoding="ascii")

	result = run("import-proto", str(source), "-o", str(tmp_path / "m.situ"))
	assert result.returncode == 1
	assert "declares no messages" in result.stderr


# -- against protoc (26.13) -------------------------------------------------


@pytest.mark.skipif(PROTOC is None, reason="protoc not installed")
def test_the_imported_schema_agrees_with_what_protoc_emits(tmp_path: Path) -> None:
	"""The acceptance criterion: vectors generated by an independent encoder.

	A schema that agrees only with situ's own reading of protobuf has
	demonstrated nothing, which is the argument the phase 6 conformance gate
	made and it holds just as well here.
	"""
	source = tmp_path / "profile.proto"
	source.write_text(TRANSLATABLE, encoding="ascii")

	message = (tmp_path / "case.txt")
	message.write_text(
		"user_id: 4886718345\n"
		'username: "situ"\n'
		"score: 1.5\n"
		"delta: -7\n"
		"seen_at: 1700000000\n"
		'tags: "a"\n'
		'tags: "b"\n'
		"status: ACTIVE\n"
		'email: "x@y"\n', encoding="ascii")

	with message.open("rb") as handle:
		encoded = subprocess.run(
			[PROTOC or "protoc", "--encode=example.wire.Profile",
			 "-I", str(tmp_path), "profile.proto"],
			stdin=handle, capture_output=True, cwd=tmp_path)
	assert encoded.returncode == 0, encoded.stderr.decode()

	seen  = _walk(encoded.stdout)
	known = _known_from(translated(TRANSLATABLE))

	assert seen, "protoc produced no fields"
	for number, wire in seen:
		assert number in known, f"protoc emitted field {number}, the schema has no entry"
		assert known[number] == wire, (
			f"field {number}: protoc used wire type {wire}, the schema says "
			f"{known[number]}")


def _known_from(schema: str) -> dict[int, int]:
	"""The field-number-to-wire-type map the imported schema declares."""
	import re

	found = {}
	for number, wire in re.findall(
			r"(\d+)\s*:\s*\{\s*name\s*=\s*\w+,\s*wire\s*=\s*(\d)", schema):
		found[int(number)] = int(wire)
	return found


def _walk(data: bytes) -> list[tuple[int, int]]:
	"""Walk a protobuf message the way the runtime's TLV cursor does.

	Deliberately a re-implementation rather than a call into the generated
	code: what is being checked is that the *schema* describes these bytes, and
	borrowing situ's decoder to prove situ's description would be circular.
	"""
	found: list[tuple[int, int]] = []
	at = 0

	while at < len(data):
		tag, at = _varint(data, at)
		number, wire = tag >> 3, tag & 7
		found.append((number, wire))

		if wire == 0:
			_, at = _varint(data, at)
		elif wire == 1:
			at += 8
		elif wire == 2:
			length, at = _varint(data, at)
			at += length
		elif wire == 5:
			at += 4
		else:
			raise AssertionError(f"wire type {wire} is not translatable")

	return found


def _varint(data: bytes, at: int) -> tuple[int, int]:
	value = 0
	shift = 0
	while True:
		byte = data[at]
		at  += 1
		value |= (byte & 0x7F) << shift
		shift += 7
		if not byte & 0x80:
			return value, at


@pytest.mark.skipif(PROTOC is None, reason="protoc not installed")
def test_protoc_accepts_the_proto_this_file_imports(tmp_path: Path) -> None:
	"""Otherwise the fixture is testing the importer against a file that is not
	valid protobuf, and every other assertion here is worthless."""
	source = tmp_path / "profile.proto"
	source.write_text(TRANSLATABLE, encoding="ascii")

	result = subprocess.run(
		[PROTOC or "protoc", "-I", str(tmp_path), "--descriptor_set_out=/dev/null",
		 "profile.proto"],
		capture_output=True, cwd=tmp_path)
	assert result.returncode == 0, result.stderr.decode()


# -- the vectors the tree already keeps -------------------------------------


def test_the_committed_example_imports_to_the_same_shape() -> None:
	"""`example/protobuf/` was written by hand from `user.proto`.

	The importer should reach the same description. Not byte-identical -- the
	hand-written one carries commentary about the five causes of non-canonicity
	that no importer would produce -- but the same field numbers, wire types
	and policies, which is what decides whether the bytes parse.
	"""
	source = (ROOT / "example" / "protobuf" / "user.proto").read_text(
		encoding="ascii")
	generated = translated(source, "user.proto")
	committed = (ROOT / "example" / "protobuf" / "protobuf.situ").read_text(
		encoding="ascii")

	assert _known_from(generated) == _known_from(committed)
	for policy in ("unknown = preserve", "duplicate_tags = allowed",
	               "tag_decode     = { field = tag >> 3, wire = tag & 0x7 }"):
		assert policy in generated and policy in committed


# -- what the fidelity report was not reporting (26.186) --------------------


def test_a_type_this_file_cannot_resolve_is_a_loss() -> None:
	"""The translation's fallback for an unknown type is "a nested message:
	length-prefixed", and that is a guess.

	The same enum field reads `wire = 0, type = colour` when the enum is
	declared here and `wire = 2, type = u8` when it comes from an import --
	and the second is wrong, because a varint is not a length-delimited run.
	Both reported zero losses, which is what 19.2 says an importer must never
	do: "an importer that silently produces a plausible-looking schema is
	worse than no importer, because the user will trust it".
	"""
	here = imported('syntax = "proto2";\nenum Colour { RED = 0; }\n'
	            'message M { optional Colour c = 1; }')
	assert not here.losses, [held.construct for held in here.losses]

	elsewhere = imported('syntax = "proto2";\nimport "colour.proto";\n'
	                 'message M { optional Colour c = 1; }')
	losses = [held.construct for held in elsewhere.losses]
	assert 'field `c` of type `Colour`' in losses, losses

	# The control that matters: a message declared in this file is a genuine
	# nested message, the fallback is right for it, and it is not a loss.
	nested = imported('syntax = "proto2";\nmessage Inner { optional int32 x = 1; }\n'
	              'message M { optional Inner i = 1; }')
	assert not nested.losses, [held.construct for held in nested.losses]


def test_more_bytes_than_this_file_describes_is_a_loss() -> None:
	"""`import`, `extensions` and `extend` each mean the wire carries fields
	this file does not list.

	All three were matched by `_RESERVED` -- one pattern named for the
	harmless case, `reserved|option|extensions|import` -- and skipped in
	silence with it.
	"""
	for source, want in (
			('import "other.proto";\nmessage M { int32 a = 1; }',
			 'import "other.proto"'),
			('message M { extensions 100 to 199; int32 a = 1; }',
			 "extensions 100 to 199"),
			('extend M { int32 b = 2; }\nmessage M { int32 a = 1; }',
			 "extend `M`")):
		losses = [held.construct for held in imported(source).losses]
		assert want in losses, f"{want} went unreported: {losses}"

	# And the two that genuinely have no wire meaning stay silent: `reserved`
	# forbids numbers nobody may use, and an `option` outside a field
	# configures a generator rather than an encoding.
	for source in ('message M { reserved 2, 15; int32 a = 1; }',
	               'message M { option (x) = true; int32 a = 1; }'):
		assert not imported(source).losses, source
