"""Two of `report.SUPPORTED`'s probes the editor did not surface (26.595).

0034 asks this frontend for *every probe `walker.report.SUPPORTED` names*,
and seventeen are named. Two were missing, and they turn out to be one
finding:

	arm_value   which arm of a variant is present
	validate    what the schema says about the whole message

`fields()` keeps FIELD and RESERVED, so a variant was dropped -- and its
arms are not members of the struct at all. For icmp, mqtt, dns and json,
whose meaning IS the variant, the tool printed a message with a hole where
the walker's own listing says `body_echo ok=1` for the same bytes.

AND THE SECOND IS WHY THE FIRST IS SAFE. `chosen_arm` answers from a
discriminant the frame does not reach by reading it as ZERO -- "which is
what the four backends do rather than a choice made here" -- so on an
empty message the arm row names the arm zero selects, truthfully, about a
message that holds none of it. `report.listing` never showed that because
its arm probe sits behind a validate that has already failed; this
frontend had no verdict at all, so the row would have read as a fact about
the bytes.

The verdict is on the header line, where it qualifies every row under it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest					# noqa: E402
from editor.document import Document, Field, open_document  # noqa: E402
from editor.text import render			# noqa: E402
from every_schema import load_schema		# noqa: E402
from situc import pack as packer			# noqa: E402
from situc.layout import solve			# noqa: E402
from situc.resolve import resolve			# noqa: E402

#: An ICMP echo request: type 8 selects the `echo` arm of six.
ECHO = bytes.fromhex("08003b5a12340001") + b"situ-abcdefgh"

#: The same with a type no case names, which `default: error` refuses.
NO_CASE = bytes([99]) + ECHO[1:]


def _image(name: str) -> bytes:
	parsed   = load_schema(ROOT / "example" / name / f"{name}.situ")
	resolved = resolve(parsed, solve(parsed))
	return packer.pack(parsed, resolved, metadata=True)[0]


def _row(document: Document, name: str) -> Field:
	return next(field for field in document.fields()
	            if field.name == name)


def test_the_variant_row_names_the_arm_that_is_present() -> None:
	"""`body` is six arms and the message selects one, which is the whole
	meaning of an ICMP message and did not appear at all."""
	held = open_document(_image("icmp"), ECHO, "icmp_message")
	body = _row(held, "body")

	assert "`echo`" in body.note, body.note
	assert "present" in body.note

	# And it reaches the reader, not just the model.
	assert any("`echo`" in line for line in render(held))


def test_a_discriminant_matching_no_case_says_so() -> None:
	"""`default: error` is a real state and a message in it is one the
	schema refuses, which is worth seeing beside the discriminant that
	chose it rather than as a row that vanishes."""
	held = open_document(_image("icmp"), NO_CASE, "icmp_message")
	body = _row(held, "body")

	assert "no arm" in body.note, body.note
	assert body.value is None


def test_the_verdict_qualifies_an_arm_read_from_bytes_that_are_absent() -> None:
	"""The case the verdict exists for.

	On an empty message every reader answers that the discriminant is zero,
	so the arm row names the arm zero selects -- truthfully, about a message
	that holds none of it. The header has to say the frame does not reach
	what the layout needs, or that row reads as a fact about the bytes.
	"""
	held = open_document(_image("icmp"), b"", "icmp_message")

	spoken = held.verdict
	assert spoken is not None and spoken[0] == "bounds", spoken

	first = render(held)[0]
	assert "[bounds]" in first, first
	assert any("validate:" in line for line in render(held))

	# The arm row is still there and still truthful: it reports what the
	# discriminant says, which is what all five readers say.
	body = _row(held, "body")
	assert "the one present" in body.note, body.note


def test_a_good_message_says_ok_once_and_explains_nothing() -> None:
	"""A verdict that needed a sentence on every message would be noise,
	and the rows below an `ok` need no qualifying."""
	held  = open_document(_image("icmp"), ECHO, "icmp_message")
	lines = render(held)

	assert held.verdict is not None and held.verdict[0] == "ok"
	assert "[ok]" in lines[0]
	assert not any("validate:" in line for line in lines), lines


def test_a_variant_in_another_schema_gets_the_same_row() -> None:
	"""mqtt, so the row is not icmp's shape read twice. Its arms are a
	payload rather than four bytes, and `publish` is what a 0x30 selects."""
	held = open_document(_image("mqtt"), bytes.fromhex("3005") + b"hello",
	                     "packet")
	body = _row(held, "body")

	assert "`publish`" in body.note, body.note
