"""A `when` compares two byte runs byte for byte, and refuses a run as a number
(26.1128).

Reported by fuzznet: `when s.old == s.successor refuse succeeded_by_itself`
over two `u8 [32]` keys reached C as `_old_get(view) == _successor_get(view)`,
a getter only a scalar has, so the unit did not compile -- and Python as a
memoryview against a memoryview, while `s.old == 3` was a memoryview against
a number, never equal, a refusal that never fired.
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
from situc.diagnostics import SituError				# noqa: E402
from situc.parser import parse_text				# noqa: E402

SCHEMA = """target buffer;
endian big;
struct succession {
	u8  old[32];
	u8  successor[32];
	u16 kind;
}
when succession.old == succession.successor
	refuse succeeded_by_itself "a key is succeeded by itself";
when succession.old != succession.successor && succession.kind == 7
	note moved "moved to another key";
"""

KEY   = bytes(range(32))
OTHER = KEY[:31] + b"\xff"

#: (bytes, verdict). The keys differ in their LAST byte, past the four a
#: number could hold, so a reader comparing a word of each says equal.
CASES = [(KEY + KEY + b"\x00\x07", "2"),
         (KEY + OTHER + b"\x00\x07", "0"),
         (OTHER + KEY + b"\x00\x01", "0")]


def _validate(said: str, struct: str) -> str:
	lines = said.splitlines()
	at = lines.index(f"-- {struct}")
	return next(line.split()[1] for line in lines[at + 1:]
	            if line.startswith("validate "))


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
def test_four_backends_compare_the_runs(tmp_path: Path) -> None:
	schema = tmp_path / "succession.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	for raw, want in CASES:
		for name, argv in command.items():
			got = _validate(fourway.answers(argv, raw, tmp_path),
			                "succession")
			assert got == want, (name, raw.hex(), got)


def test_the_image_says_it_cannot_carry_the_comparison() -> None:
	"""An image compares numbers; loading a run reads a word of it, so the
	`when` is recorded as unencodable rather than answered from one."""
	from situc.layout import solve
	from situc.pack import pack
	from situc.resolve import resolve
	parsed = parse_text(SCHEMA)
	_, coverage = pack(parsed, resolve(parsed, solve(parsed)))
	assert "when succeeded_by_itself" in coverage.unencodable, \
		coverage.unencodable
	assert "byte runs" in coverage.unencodable["when moved"]


REFUSED = [
	("succession.old == 3", "compared with something that is not a run"),
	("succession.old + 1 == succession.successor",
	 "compared with something that is not a run"),
	("succession.old == succession.short", "never equal"),
	("succession.old > succession.successor", "read here as a number"),
	("succession.wide == succession.wide", "stated length"),
]


@pytest.mark.parametrize("rule, label", REFUSED)
def test_a_run_read_as_a_number_is_refused(rule: str, label: str) -> None:
	text = ("endian big;\nstruct succession {\n\tu8 old[32];\n"
	        "\tu8 successor[32];\n\tu8 short[31];\n\tu16 wide[16];\n}\n"
	        f"when {rule} refuse bad \"bad\";\n")
	with pytest.raises(SituError) as caught:
		parse_text(text)
	shown = caught.value.diagnostic.render()
	assert "is a run, and a `when` reads numbers" in shown, shown
	assert label in shown, shown


def test_a_run_measured_stays_a_number() -> None:
	text = ("endian big;\nstruct s {\n\tu8 n;\n\tu8 data[n];\n}\n"
	        "when size(s.data) > 4 note long \"long\";\n")
	parse_text(text)


@pytest.mark.skipif(shutil.which("tshark") is None
                    or shutil.which("text2pcap") is None,
                    reason="needs tshark and text2pcap")
def test_tshark_compares_the_runs(tmp_path: Path) -> None:
	"""The real consumer: thirty-two bytes are past what `uint` reads."""
	source = tmp_path / "succession.situ"
	source.write_text(SCHEMA, encoding="ascii")
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                "--out", str(tmp_path), str(source)],
	               check=True, capture_output=True, cwd=ROOT)
	script = tmp_path / "unit.lua"
	script.write_text((tmp_path / "succession.lua").read_text("ascii")
	                  + 'DissectorTable.get("wtap_encap")'
	                  ":add(wtap.USER0, succession)\n", encoding="ascii")
	dump = tmp_path / "keys.hex"
	dump.write_text("".join(f"0000 {raw.hex(' ')}\n\n"
	                        for raw, _ in CASES)
	                + f"0000 {KEY[:20].hex(' ')}\n", encoding="ascii")
	capture = tmp_path / "keys.pcap"
	subprocess.run(["text2pcap", "-q", "-l", "147", str(dump), str(capture)],
	               check=True, capture_output=True)
	shown = subprocess.run(["tshark", "-r", str(capture),
	                        "-X", f"lua_script:{script}", "-V"],
	                       capture_output=True, text=True, timeout=120)
	assert "Lua: Error" not in shown.stderr, shown.stderr
	frames = shown.stdout.split("\nFrame ")
	assert len(frames) == 4, shown.stdout
	said = ["succeeded by itself" in frame for frame in frames]
	assert said == [True, False, False, False], shown.stdout
	assert "moved to another key" in frames[1], frames[1]
	assert "moved to another key" not in frames[2], frames[2]
