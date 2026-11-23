"""A tlv item the schema knows is named in Wireshark, and a struct-typed
value is dissected as that struct (26.1167).

Each item was a row of its bytes alone, so the `date` of a region that
says `1 : { name = date, type = tv }` reached Wireshark as `0102170b`,
the name and the type the schema gives it nowhere. The row now carries
the name, and a value of a struct type is that struct's subtree under
it, as a member placed by `at` is (26.978). An unknown tag is left as
its bytes.

Run in tshark itself: the stub cannot say what `Dissector.get` finds.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse

SCHEMA = """target buffer;
endian big;
struct tv { u8 day; u8 month [max = 12]; }
struct opts {
	tlv items (tag_type = u8, length_type = u8,
	           known = { 1 : { name = date, type = tv },
	                     2 : { name = note } },
	           unknown = skip, duplicate_tags = allowed);
}
"""


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_a_known_item_is_named_and_a_typed_one_dissected(
		tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	script = tmp_path / "unit.lua"
	script.write_text(generate(parse(source), resolved, "unit")
	                  + 'DissectorTable.get("wtap_encap")'
	                  ":add(wtap.USER0, opts)\n", encoding="ascii")
	dump = tmp_path / "p.hex"
	# date 23/11, note "abc", and tag 7, which nothing names.
	dump.write_text("0000 01 02 17 0b 02 03 61 62 63 07 01 ff\n",
	                encoding="ascii")
	capture = tmp_path / "p.pcap"
	subprocess.run(["text2pcap", "-q", "-l", "147", str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run(["tshark", "-r", str(capture),
	                        "-X", f"lua_script:{script}", "-V"],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua Error" not in shown.stdout + shown.stderr, shown.stdout
	lines = [line.strip() for line in shown.stdout.splitlines()]
	at = lines.index("items: 0102170b (date)")
	assert lines[at + 1:at + 4] == ["unit.tv (situ)", "day: 23",
	                                "month: 11"], lines[at:at + 4]
	assert "items: 0203616263 (note)" in lines
	assert "items: 0701ff" in lines
