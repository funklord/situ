"""Every placement kind reaches a row in the reference frontend (26.597).

`fields()` filtered on `kind not in (FIELD, RESERVED)` and skipped the rest.
`std/image.situ` declares EIGHT kinds and `walker/report.py` named four, so
the allow-list that follows from a short table dropped every member of the
other four. Swept over the corpus: 18 members across 12 structs -- and for
`slip` and `protobuf`, whose whole message is one such member, the tool
printed a header and no rows at all.

So the module pins three things rather than one:

	the reader's kind table against the enum the schema declares
	every member of every struct reaching a row, over the whole corpus
	what each kind's row says, where saying it took a reader

The first is the one that stops this recurring. A table short by four is
not a defect anybody trips over -- the four kinds it omits simply go
unmentioned -- so nothing but a comparison against the schema can see it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest						# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from situc import ast				# noqa: E402
from editor.document import Field, open_document	# noqa: E402
from editor.text import render				# noqa: E402
from walker import report				# noqa: E402
from walker.walk import (Refused, Unsupplied, acquire,	# noqa: E402
                         marker_order)
from every_schema import SCHEMAS, ids			# noqa: E402

#: Arbitrary but fixed. The question here is which members reach a ROW, and
#: a row's absence is what this can see -- so the bytes need only be the
#: same bytes for the walker and the editor, not meaningful to any format.
BUF = bytes((i * 37 + 11) & 0xFF for i in range(512))


def packed(path: Path) -> bytes:
	parsed   = parser.parse(Source(str(path), path.read_text(encoding="utf-8")))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return blob


def rows(image_bytes: bytes, struct: str) -> dict[str, Field]:
	document = open_document(image_bytes, BUF, struct)
	return {field.name: field for field in document.fields()}


def note_of(image_bytes: bytes, struct: str, name: str) -> str:
	return rows(image_bytes, struct)[name].note or ""


def test_the_readers_kind_table_matches_the_schemas_enum() -> None:
	"""`std/image.situ` is where `image_kind` is declared; the constants in
	`walker/report.py` are a copy of it, and a copy short by four is what
	produced the allow-list this module exists for.

	The enum's `default = error` is deliberately not a member: an unknown
	kind is a refusal there, which is why the reader's list may be checked
	for equality rather than for containment.
	"""
	schema = parser.parse(Source(
		"std/image.situ",
		(ROOT / "std" / "image.situ").read_text(encoding="utf-8")))
	declared = [decl for decl in schema.enums() if decl.name == "image_kind"]
	assert len(declared) == 1, "one `image_kind` enum, or this test is lost"

	theirs = {}
	for member in declared[0].members:
		# Narrowed rather than trusted: the enum's values are expressions,
		# and one written as anything but a literal is a case this
		# comparison cannot make -- so it says so instead of reading
		# whatever attribute happens to be there.
		assert isinstance(member.value, ast.IntLiteral), member.name
		theirs[member.name] = member.value.value
	ours = {
		"field":   report.FIELD,
		"reserved": report.RESERVED,
		"marker":  report.MARKER,
		"region":  report.REGION,
		"variant": report.VARIANT,
		"tlv":     report.TLV,
		"indexed": report.INDEXED,
		"opaque":  report.OPAQUE,
	}
	assert theirs == ours


def test_every_sentence_names_a_kind_the_schema_declares() -> None:
	"""`KIND_SAYS` is keyed by number, so a typo is a sentence no row can
	reach -- which looks exactly like a kind nothing has had to explain."""
	known = {report.FIELD, report.RESERVED, report.MARKER, report.REGION,
	         report.VARIANT, report.TLV, report.INDEXED, report.OPAQUE}
	assert set(report.KIND_SAYS) <= known
	assert report.FIELD not in report.KIND_SAYS
	assert report.RESERVED not in report.KIND_SAYS


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_every_member_of_every_struct_reaches_a_row(path: Path) -> None:
	"""The sweep that found it, as an assertion.

	A member the walk cannot place still gets a row saying so, so there is
	no kind of member for which an absent row is the right answer -- which
	is what makes this an equality rather than a judgement.
	"""
	image_bytes = packed(path)
	from walker.image import load
	image = load(image_bytes)
	for index in range(len(image.structs)):
		name = image.struct_name(index)
		try:
			acquire(image, BUF, index)
		except (Refused, Unsupplied):
			continue		# no view: `report` says so too, and prints no member
		members = image.members(image.structs[index])
		wanted = {image.name_of(one).rpartition(".")[2] or image.name_of(one)
		          for one in members}
		assert wanted <= set(rows(image_bytes, name)), (
			f"{path.name}:{name} drops "
			f"{sorted(wanted - set(rows(image_bytes, name)))}")


def test_a_marker_row_reads_its_value_and_names_the_order() -> None:
	"""TIFF's first two bytes decide how every other field in the struct is
	read, and they had no row. The value is the marker's own, read big
	whatever it says, and the order is `walk.marker_order`'s answer rather
	than a second comparison made here."""
	image_bytes = packed(ROOT / "example" / "tiff" / "tiff.situ")
	field = rows(image_bytes, "tiff_header")["byte_order"]
	assert field.offset == 0
	assert field.value is not None, "the marker is readable and was not read"

	from walker.image import load
	image = load(image_bytes)
	index = [image.struct_name(i)
	         for i in range(len(image.structs))].index("tiff_header")
	said = marker_order(acquire(image, BUF, index))
	assert said in (field.note or "")


def test_a_message_that_is_one_region_is_not_an_empty_listing() -> None:
	"""`slip`'s whole frame is one region. The listing was a header and
	nothing else, which is indistinguishable from a tool that failed."""
	image_bytes = packed(ROOT / "example" / "slip" / "slip.situ")
	held = rows(image_bytes, "frame")
	assert set(held) == {"datagram"}
	assert held["datagram"].value, "82 bytes the walker reads, and no value"
	assert "region" in (held["datagram"].note or "")

	# And it reaches the rendering, not just the model: an empty listing was
	# what a person saw.
	drawn = render(open_document(image_bytes, BUF, "frame"))
	assert any("datagram" in line for line in drawn)


def test_a_tlv_run_reaches_a_row() -> None:
	"""`protobuf`'s whole message is one tlv run -- kind 5, which the
	reader's table did not name at all."""
	image_bytes = packed(ROOT / "example" / "protobuf" / "protobuf.situ")
	held = rows(image_bytes, "proto_message")
	assert set(held) == {"fields"}
	assert "tag-length-value" in (held["fields"].note or "")


def test_an_indexed_run_keeps_the_kind_and_the_instruction() -> None:
	"""Two notes on one row, which is what caught the nested-struct branch
	assigning where every other branch appends: the kind sentence was
	written and then overwritten by the instruction."""
	image_bytes = packed(ROOT / "example" / "sqlite" / "sqlite.situ")
	note = note_of(image_bytes, "btree_leaf_page", "cells")
	assert "index table" in note
	assert "--offset" in note


def test_a_span_the_frame_does_not_hold_says_cannot_be_read() -> None:
	"""`dtls`'s sealed region is as long as its length field says, which
	these bytes make longer than the message. The row is still there, and
	its value is not invented."""
	image_bytes = packed(ROOT / "example" / "dtls" / "dtls.situ")
	field = rows(image_bytes, "record")["sealed"]
	assert field.value is None
	assert "cannot be read" in (field.note or "")
	assert "region" in (field.note or "")


def test_a_kind_with_no_sentence_still_reaches_a_row(
		monkeypatch: pytest.MonkeyPatch) -> None:
	"""The behaviour that cannot go stale as the enum grows. A ninth kind
	is refused by `std/image.situ` itself, so what this covers is the gap
	between a kind arriving and anybody writing its sentence -- which is
	exactly the window the allow-list turned into a silent drop."""
	monkeypatch.setattr(report, "KIND_SAYS", {})
	image_bytes = packed(ROOT / "example" / "tiff" / "tiff.situ")
	note = note_of(image_bytes, "tiff_header", "byte_order")
	assert "kind 2" in note
	assert "no sentence" in note
