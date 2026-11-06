"""`[align = n]`, a pad at a run-time offset, and what they found (26.889).

Each test here is a fault that shipped or a refusal that had to be written: the
bytes after a pad read from the wrong place in three backends, two pads in one
struct not compiling, a pad arm costing every later member its accessor. The
format they came from is `example/radiotap`, and these are its shapes alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import ast
from situc.diagnostics import SituError
from situc.parser import parse_text
from situc.unparse import unparse

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\nstruct nothing { }\n"


def four_say(tmp_path: Path, text: str, *packets: bytes) -> list[str]:
	source = tmp_path / "unit.situ"
	source.write_text(text, encoding="ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	said = []
	for packet in packets:
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, (packet.hex(), given)
		said.append(given["c"])
	return said


def refusal(text: str) -> str:
	with pytest.raises(SituError) as caught:
		parse_text(HEAD + text, path="s.situ")
	return caught.value.diagnostic.render()


def test_a_member_s_alignment_is_a_pad_and_the_member() -> None:
	schema = parse_text(HEAD + "struct p { u8 a; u16 b [align = 4]; }\n",
	                    path="s.situ")
	members = schema.structs()[-1].members
	pad, field = members[1], members[2]
	assert isinstance(pad, ast.Pad) and pad.to == 4
	assert isinstance(field, ast.Field)
	assert not [one for one in field.attrs if one.name == "align"]


def test_an_arm_s_alignment_is_a_variant_of_pads_in_front() -> None:
	schema = parse_text(HEAD + "struct p { u8 f; variant v switch (f) {\n"
	                    "\tcase 1: u16 b [align = 4];\n"
	                    "\tcase 0: nothing none;\n\tdefault: error; } }\n",
	                    path="s.situ")
	members = schema.structs()[-1].members
	assert [getattr(one, "name", None) for one in members[1:]] == ["v_align", "v"]
	pads = members[1]
	assert isinstance(pads, ast.Variant)
	assert [getattr(arm.member, "to", None) for arm in pads.arms] == [4, 1, None]


def test_the_unparser_writes_what_was_lowered() -> None:
	text = HEAD + "struct p { u8 a; u16 b [align = 4]; }\n"
	once = unparse(parse_text(text, path="s.situ"))
	assert once == unparse(parse_text(once, path="s.situ"))


@pytest.mark.parametrize("value", ["0", "n"])
def test_an_alignment_is_a_positive_literal(value: str) -> None:
	assert "positive literal alignment" in refusal(
		f"struct p {{ u8 n; u16 b [align = {value}]; }}\n")


def test_a_pad_beside_bytes_at_a_run_time_offset_is_refused() -> None:
	from situc.layout import solve
	with pytest.raises(SituError) as caught:
		solve(parse_text(HEAD + "struct p { u8 k; u8 n; u8 r[n];\n"
		                 "\tvariant v switch (k) { case 1: pad_to(4);\n"
		                 "\tdefault: u8 other; } u8 after; }\n", path="s.situ"))
	said = caught.value.diagnostic.render()
	assert "holds bytes beside an arm that pads" in said
	assert "[align = n]" in said


def test_preserve_reaches_a_pad() -> None:
	"""0043 documents it, and the placement table refused it."""
	parse_text(HEAD + "struct p { u8 a; pad_to(4) [preserve]; u8 b; }\n",
	           path="s.situ")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_bytes_after_a_pad_are_where_the_pad_left_them(tmp_path: Path) -> None:
	"""C++, Rust and Python aligned a total holding the bytes behind the pad."""
	said = four_say(
		tmp_path, HEAD + "struct p { u8 n; u8 a[n]; pad_to(4); u16 x; u8 y;"
		" u8 z[n]; pad_to(4); u8 w; }\n",
		bytes([3, 1, 2, 3, 0, 7, 9, 1, 2, 3, 0, 0, 0xAA]))
	assert "y 9" in said[0]
	assert "w 170" in said[0]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_nonzero_pad_past_a_run_is_refused_by_all(tmp_path: Path) -> None:
	said = four_say(
		tmp_path, HEAD + "struct p { u8 n; u8 a[n]; pad_to(4); u16 x; u8 y;"
		" u8 z[n]; pad_to(4); u8 w; }\n",
		bytes([3, 1, 2, 3, 0, 7, 9, 1, 2, 3, 0xEE, 0, 0xAA]))
	assert "validate 0" not in said[0]


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_a_pad_arm_at_a_run_time_offset_keeps_what_follows(
		tmp_path: Path) -> None:
	"""Every member after one lost its accessor while `validate` called it."""
	text = HEAD + ("struct p { u8 f; u8 n; u8 a[n];\n"
	               "\tvariant v switch (f) {\n"
	               "\t\tcase 1: u16 w [align = 4];\n"
	               "\t\tcase 0: nothing none;\n"
	               "\t\tdefault: error; }\n"
	               "\tu8 after; }\n")
	aligned, plain = four_say(tmp_path, text,
	                          bytes([1, 1, 9, 0, 0xAB, 0xCD, 7]),
	                          bytes([0, 1, 9, 7]))
	assert "after 7" in aligned
	assert "after 7" in plain
