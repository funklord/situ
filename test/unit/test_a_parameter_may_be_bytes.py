"""`parameter u8 iv[12];` is bytes the caller holds (26.887).

A 12-byte AES-GCM nonce could not be supplied out of band, a parameter being
a scalar. Material is named for a codec, width-checked against it, and read
by nothing, so no view carries it: the caller hands it to their own primitive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc import advise, ast, capmap, traverse, wire
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import ResolvedSchema, resolve
from situc.unparse import unparse

CODEC = ("codec ae { tag_bytes = 16; nonce_bytes = 12; key_bytes = 32;\n"
         "\tlength_preserving; seekable = linear; granularity = byte;\n"
         "\tauthenticated; invertible; deterministic; }\n")
HEAD  = "target buffer;\nendian big;\n" + CODEC


def message(lead: str = "parameter u8 iv[12];", nonce: str = "iv",
            key: str = "") -> str:
	bind = f", key = {key}" if key else ""
	return (HEAD + f"struct s {{ {lead} u8 kind;\n"
	        f"\tsealed(ae, nonce = {nonce}{bind}) {{ u8 body[kind]; }}\n"
	        "\ttag u8[16]; }\n")


def compiled(text: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse_text(text, path="s.situ")
	return schema, resolve(schema, solve(schema))


def refusal(text: str) -> str:
	with pytest.raises(SituError) as caught:
		compiled(text)
	return caught.value.diagnostic.render()


def test_the_map_and_the_wire_name_it() -> None:
	schema, held = compiled(message())
	assert "parameter bytes=12" in capmap.render(schema, held, "s.situ")
	signature = wire.render(schema, held, "s.situ")
	assert "u8[12]" in signature and "nonce=iv" in signature


def test_it_is_not_an_argument() -> None:
	"""No view takes it: the whole of why it is not a placement."""
	schema = parse_text(message(key=""), path="s.situ")
	assert traverse.parameters(schema) == []


def test_a_const_may_size_it() -> None:
	compiled(message("parameter u8 iv[NONCE];").replace(
		"struct s", "const NONCE = 12;\nstruct s"))


def test_the_width_is_held_against_the_codec() -> None:
	said = refusal(message("parameter u8 iv[8];"))
	assert "`iv` is 8 bytes and `ae` takes a 12-byte nonce" in said


def test_a_const_width_is_held_too() -> None:
	said = refusal(message("parameter u8 iv[NONCE];").replace(
		"struct s", "const NONCE = 8;\nstruct s"))
	assert "takes a 12-byte nonce" in said


def test_key_material_is_held_against_key_bytes() -> None:
	compiled(message("parameter u8 iv[12]; parameter u8 k[32] [secret];",
	                 key="k"))
	said = refusal(message("parameter u8 iv[12]; parameter u8 k[16];",
	                       key="k"))
	assert "`k` is 16 bytes and `ae` takes a 32-byte key" in said


def test_a_selector_is_not_held_against_key_bytes() -> None:
	"""The control: `key = epoch` chooses a key and is not one."""
	compiled(message("parameter u8 iv[12]; u8 epoch;", key="epoch"))


def test_a_byte_run_in_the_message_still_selects_nothing() -> None:
	"""0040's refusal stands for bytes that are not the key."""
	said = refusal(message("parameter u8 iv[12]; u8 run[4];", key="run"))
	assert "no value to select by" in said


@pytest.mark.parametrize("lead, expected", [
	("parameter u16 iv[6];",                  "must be a run of `u8`"),
	("parameter u8 iv[remaining];",           "needs a size the schema fixes"),
	("u8 n; parameter u8 iv[n];",             "needs a size the schema fixes"),
	("parameter u8 iv[];",                    "needs a size the schema fixes"),
	("parameter u8 iv[12] [stream];",         "means nothing on `parameter iv`"),
	("parameter u8 iv[12] [must_eq = 3];",    "`[must_eq]`"),
])
def test_what_it_may_not_be(lead: str, expected: str) -> None:
	assert expected in refusal(message(lead))


@pytest.mark.parametrize("member", [
	"u8 a[iv];", "u8 a [max = iv];", "u8 a at iv;",
])
def test_nothing_reads_it(member: str) -> None:
	said = refusal(message("parameter u8 iv[12]; " + member))
	assert "bytes the caller holds, with no value to read" in said


def test_a_parameter_in_an_arm_is_still_refused() -> None:
	said = refusal(HEAD + "struct s { u8 t; variant v switch (t) {\n"
	               "\tcase 1: parameter u8 iv[12];\n\tdefault: opaque; } }\n")
	assert "cannot be declared in a variant arm" in said


def test_a_struct_holding_only_material_nests_with_no_argument() -> None:
	compiled(HEAD + "struct inner { parameter u8 iv[12]; u8 x; }\n"
	         "struct outer { inner a; u8 b; }\n")


def test_it_round_trips_through_the_unparser() -> None:
	source = unparse(parse_text(message(), path="s.situ"))
	assert "parameter u8 iv[12];" in source
	assert "key_bytes = 32;" in source


def test_the_advisor_reads_a_short_nonce() -> None:
	"""It costs nothing on the wire, since the bytes are not there."""
	_, held = compiled(message("parameter u8 iv[4];").replace(
		"nonce_bytes = 12; ", ""))
	found = [one for one in advise.suggest(held)
	         if one.rule == "widen-a-short-nonce"]
	assert len(found) == 1
	assert found[0].subject == "s.iv"
	assert found[0].cost.worst == 0


def test_a_change_of_width_changes_the_wire() -> None:
	"""The signature is the contract, and a peer must hold the same bytes."""
	text = HEAD.replace("nonce_bytes = 12; ", "") \
		+ "struct s { parameter u8 iv[8]; u8 x; }\n"
	was = compiled(text)
	now = compiled(text.replace("[8]", "[12]"))
	assert wire.render(*was, "s") != wire.render(*now, "s")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_the_four_build_and_agree_with_no_argument(tmp_path: Path) -> None:
	"""Every backend reads the message as though the bytes were not there."""
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + "struct p { parameter u8 iv[12]; u8 len;\n"
	                  "\tu8 data[len]; u16 tail; }\n", encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert len(command) == 4
	for packet in (bytes([2, 9, 9, 0, 7]), bytes([200, 1])):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		assert len(set(given.values())) == 1, given
	assert "len=2" in fourway.answers(command["c"], bytes([2, 9, 9, 0, 7]),
	                                  tmp_path)
