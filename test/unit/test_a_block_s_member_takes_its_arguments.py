"""An argued member inside a `positional` or `authenticated` block (0068).

    struct pos { u16 block; positional { note(mode = 1) k; u8 z; }
                 entry(block = block) e; u8 after; }
    struct au  { u16 block; authenticated { entry(block = block) e;
                 u8 after; } tag u8[16] covers(authenticated); }

Both blocks hold the struct's own members, placed as though the block
were not there -- one asserts staticness and the other marks what a tag
covers -- so a member inside one is built exactly as steps 2 to 4 build
one outside. Only the refusal had to move. A sealed or coded region's
interior is a transform's input, and stays refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError
from situc.parser import parse_text
from test_dissector import LUA, compare
from test_walker_c import (COMPILER, _inline_image, c_verdict,
                           python_verdict, shape_named_text)

SCHEMA = """target buffer;
endian big;
struct entry { parameter u16 block [stream]; u8 tag [max = 5]; u8 data[block]; }
struct note { parameter u8 mode [stream]; u8 v; }
struct pos { u16 block; positional { note(mode = 1) k; u8 z; }
             entry(block = block) e; u8 after; }
struct au { u16 block; authenticated { entry(block = block) e; u8 after; }
            tag u8[16] covers(authenticated); }
"""

TAG = "00" * 16
CASES = {
	"pos": {"0001010002aa05": "0", "0001010007aa05": "2",
	        "0001010002aa": "1"},
	"au":  {"000102aaee" + TAG: "0", "000107aaee" + TAG: "2"},
}


def _validate(answers: str, name: str) -> str:
	section = answers[answers.index(f"-- {name}\n"):].split("\n-- ")[0]
	return next(line.split()[1] for line in section.splitlines()
	            if line.startswith("validate"))


@pytest.mark.skipif(not fourway.COMPLETE or COMPILER is None,
                    reason="needs gcc, g++ and rustc")
def test_six_readers_agree(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	blob = _inline_image(SCHEMA)
	for name, cases in CASES.items():
		shape = shape_named_text(SCHEMA, name)
		for message, verdict in cases.items():
			packet = bytes.fromhex(message)
			answers = [fourway.answers(argv, packet, tmp_path)
			           for argv in command.values()]
			assert len({answer[answer.index(f"-- {name}\n"):]
			            .split("\n-- ")[0] for answer in answers}) == 1, \
				(name, message)
			assert _validate(answers[0], name) == verdict, (name, message)
			assert python_verdict(blob, packet, shape) == verdict
			assert c_verdict(tmp_path, blob, packet, shape) == verdict


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_dissector_agrees(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert not compare(schema).differ


def test_a_sealed_interior_is_still_refused() -> None:
	with pytest.raises(SituError) as refused:
		parse_text("target buffer;\nendian big;\n"
		           "codec aes_gcm_128 { tag_bytes = 16; nonce_bytes = 12;"
		           " granularity = byte; length_preserving; seekable;"
		           " authenticated; invertible; deterministic; }\n"
		           "struct note { parameter u8 mode; u8 v; }\n"
		           "struct s { u8 nonce[12]; sealed(aes_gcm_128, nonce = nonce)"
		           " { note(mode = 1) k; } tag u8[16]; }\n")
	assert "cannot be a member yet" in str(refused.value)
