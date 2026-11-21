"""A `when` may read a `let` of its struct (26.1102).

The README says a `let` is "usable by any expression after it", and a
`when` refused one as a field that does not exist. A let is computed
modulo 2^64 and held to its width, so it is not inlined: every reader asks
the let itself -- the four backends through its getter, both walkers
through a `LET` op naming its row in the lets section.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402

SCHEMA = """target buffer;
endian big;
struct wide {
	u8 a;
	u8 b;
	let u16 total = a + b;
}
struct narrow {
	u8 a;
	u8 b;
	let u8 total = a + b;
}
struct signed_let {
	u8 a;
	u8 b;
	let i8 diff = a - b;
}
when wide.total > 300
	refuse wide_too_big "a and b may not sum past 300";
when narrow.total > 10
	refuse narrow_too_big "held to a byte, the sum may not pass 10";
when signed_let.diff < 0
	refuse backwards "b may not exceed a";
"""

#: (struct, bytes, verdict). `ff ff` is 510 to the u16 and 254 to the u8 --
#: the width is the let's, not the arithmetic's.
CASES = [("wide", b"\x01\x02", "0"), ("wide", b"\xff\xff", "2"),
         ("narrow", b"\x01\x02", "0"), ("narrow", b"\x06\x06", "2"),
         ("narrow", b"\xff\x02", "0"),		# 257 held to a byte is 1
         ("signed_let", b"\x05\x02", "0"), ("signed_let", b"\x02\x05", "2"),
         ("signed_let", b"\xff\x00", "2")]	# 255 as an i8 is -1


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_read_the_let(tmp_path: Path) -> None:
	schema = tmp_path / "lets.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for struct, raw, want in CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path), struct)
			assert got == want, (name, struct, raw.hex(), got)


def test_both_walkers_read_the_let(tmp_path: Path) -> None:
	from test_walker_c import (COMPILER, VERDICT, _build_driver, c_verdict,
	                           packed_text, python_verdict, shape_named_text)
	blob  = packed_text(SCHEMA)
	drive = (_build_driver(tmp_path, blob, VERDICT)
	         if COMPILER is not None else None)
	for struct, raw, want in CASES:
		shape = shape_named_text(SCHEMA, struct)
		assert python_verdict(blob, raw, shape) == want, \
			("python", struct, raw.hex())
		if drive is not None:
			assert c_verdict(tmp_path, blob, raw, shape, drive) == want, \
				("c walker", struct, raw.hex())


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_tshark_reads_the_let_in_a_when(tmp_path: Path) -> None:
	"""The dissector wrote each rule out as a comment (26.1133): it reads
	every let below 64 bits as a number now, the same cases as above."""
	source = tmp_path / "lets.situ"
	source.write_text(SCHEMA, encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector", "--out",
	                str(tmp_path), str(source)],
	               check=True, capture_output=True, cwd=ROOT)
	names = {"wide": "wide_too_big", "narrow": "narrow_too_big",
	         "signed_let": "backwards"}
	for struct, raw, want in CASES:
		script = tmp_path / f"{struct}.lua"
		script.write_text((tmp_path / "lets.lua").read_text("ascii")
		                  + 'DissectorTable.get("wtap_encap")'
		                  f":add(wtap.USER0, {struct})\n", encoding="ascii")
		dump = tmp_path / "one.hex"
		dump.write_text(f"0000 {raw.hex(' ')}\n", encoding="ascii")
		capture = tmp_path / "one.pcap"
		subprocess.run(["text2pcap", "-q", "-l", "147", str(dump),
		                str(capture)], check=True, capture_output=True)
		shown = subprocess.run(["tshark", "-r", str(capture), "-X",
		                        f"lua_script:{script}", "-V"],
		                       capture_output=True, text=True, timeout=120)
		assert "Lua: Error" not in shown.stderr, shown.stderr
		said = names[struct] in shown.stdout or "may not" in shown.stdout
		assert said == (want == "2"), (struct, raw.hex(), shown.stdout)
