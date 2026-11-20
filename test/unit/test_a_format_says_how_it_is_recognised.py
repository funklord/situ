"""`identify`: how a file is recognised as a format (26.1124).

The verdicts are held to levels worked by hand from the rules in
`situc/identify.py`, and the magic(5) export to what file(1) itself says
about the same bytes -- a stand-in cannot refuse for the real consumer.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc import identify
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target file;\nendian big;\nbit_order msb_first;\n"

#: A PNG-like head: the signature, then the first chunk's length, type and
#: the start of IHDR -- all at fixed offsets.
HEAD = PREAMBLE + r"""
enum kind : u8[4] { ihdr = "IHDR", idat = "IDAT", default = pass, }
enum colour : u8 { grey = 0, rgb = 2, palette = 3, }
struct sig { preamble u8[8] = "\x89PNG\r\n\x1a\n"; }
struct chunk { u32 length [max = 100]; kind type; u32 width; u32 height;
               u8 depth; colour col; }
struct head { sig signature; chunk first; }
"""

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, depth: int = 8, colour: int = 2) -> bytes:
	return (SIGNATURE + b"\x00\x00\x00\x0d" + kind + b"\x00\x00\x00\x01"
	        b"\x00\x00\x00\x01" + bytes([depth, colour]))


def _formats(source: str) -> list[identify.Recogniser]:
	schema = parse_text(source)
	return identify.recognisers(schema, resolve(schema, solve(schema)))


def _level(source: str, head: bytes, name: str) -> str:
	(one,) = _formats(source)
	return one.judge(head, name)[0]


GATED = HEAD + """
identify png(f: head) [media = "image/png"] {
	sufficient f.signature && f.first.type == ihdr;
	required   f.first.depth <= 16;
	supporting extension "png";
}
"""

@pytest.mark.parametrize("head,name,level", [
	# The signature and IHDR: certain, whatever the name.
	(_chunk(b"IHDR"), "a.bin", "certain"),
	# The required bound fails: no, though the sufficient item holds.
	(_chunk(b"IHDR", depth=200), "a.png", "no"),
	# No IHDR, the bound holds, and the name supports it: likely.
	(_chunk(b"IDAT"), "a.png", "likely"),
	# The same with no supporting name: possible.
	(_chunk(b"IDAT"), "a.bin", "possible"),
	# Text named .png: byte 24 is a space (32), the bound fails: no.
	(b"hello world, this is not a png file....", "a.png", "no"),
	# Not PNG at all, but byte 24 is 8: the weak bound holds and the name
	# supports it, so likely -- exactly what a weak `required` buys, and
	# why PNG's own schema makes the signature sufficient instead.
	(b"x" * 24 + b"\x08", "a.png", "likely"),
	# Too short to read the bound: no.
	(b"\x89PNG", "a.png", "no"),
])
def test_the_levels_follow_the_roles(head: bytes, name: str,
		level: str) -> None:
	assert _level(GATED, head, name) == level


def test_a_supporting_item_never_stands_alone() -> None:
	"""The first version answered "likely PNG" for a text file named
	`a.png`, against the PNG schema's own comment."""
	source = HEAD + """
identify png(f: head) {
	sufficient f.signature;
	supporting extension "png";
}
"""
	assert _level(source, b"not a png at all, merely named one", "a.png") \
		== "no"
	assert _level(source, _chunk(b"IHDR"), "a.bin") == "certain"


def test_an_extension_can_be_the_proof() -> None:
	"""A format with nothing better than its name says so by role."""
	source = HEAD + """
identify raw(f: head) { sufficient extension "raw", "RAW"; }
"""
	assert _level(source, b"anything", "Photo.Raw") == "certain"
	assert _level(source, b"anything", "photo.jpg") == "no"


def test_a_bare_member_is_its_own_constraint() -> None:
	"""`f.first.col` is a closed enum: its members are the evidence, and
	nothing restates them."""
	source = HEAD + """
identify png(f: head) { required f.first.col; }
"""
	assert _level(source, _chunk(b"IHDR", colour=3), "x") == "possible"
	assert _level(source, _chunk(b"IHDR", colour=1), "x") == "no"


def test_the_most_pinned_wins_inside_a_level() -> None:
	"""Two formats certain on the same bytes: the one whose evidence
	pinned more bits is chosen -- a tiebreak, never a score."""
	formats = (_formats(HEAD + "identify loose(f: head) "
	                    "{ sufficient f.signature; }")
	           + _formats(HEAD + "identify tight(f: head) "
	                      "{ sufficient f.signature && f.first.type == ihdr; }"))
	chosen, level = identify.best(formats, _chunk(b"IHDR"), "x")
	assert (chosen and chosen.name, level) == ("tight", "certain")
	chosen, level = identify.best(formats, _chunk(b"IDAT"), "x")
	assert (chosen and chosen.name, level) == ("loose", "certain")


TEXTS = PREAMBLE + """
identify text { required text utf8; }
identify csv { required text csv; }
identify json { required text json; }
identify ascii { required text ascii; }
"""

@pytest.mark.parametrize("head,expected", [
	(b'a,"b,c",d\n1,2,3\n', "csv"),       # the quoted comma is not one
	(b"a,b\n1,2,3\n", "ascii"),           # unequal: not CSV
	(b"a,b\n", "ascii"),                   # one line is not enough
	(b"\xef\xbb\xbf\n [1]", "json"),       # behind a BOM and whitespace
	(b"h\xc3\xa9llo", "text"),              # UTF-8, not ASCII
	(b"x" * 4095 + b"\xe2", "text"),        # cut by a full window
	(b"x\xe2\x82", "-"),                    # cut by the file's own end
	(b"\xe0\x80\x80", "-"),                 # overlong
	(b"\xed\xa0\x80", "-"),                 # a surrogate
	(b"bell\x07", "-"),                     # a control byte
	(b"", "-"),
])
def test_the_text_heuristics(head: bytes, expected: str) -> None:
	chosen, _ = identify.best(_formats(TEXTS), head, "x")
	assert (chosen.name if chosen else "-") == expected


def test_head_is_the_bytes_the_evidence_needs() -> None:
	(one,) = _formats(GATED)
	assert one.head == 25


@pytest.mark.parametrize("body,refusal", [
	("required f.first.width == f.first.height;", "must be a number"),
	("required f.nothing;", "names no field"),
	("required g.first.depth;", "names no field of `f`"),
	("required f.first.width;", "states nothing to check"),
	("supporting extension \"png\";", "nothing `sufficient` or `required`"),
])
def test_what_the_first_tier_cannot_read_is_refused(body: str,
		refusal: str) -> None:
	with pytest.raises(SituError, match=refusal):
		_formats(HEAD + f"identify png(f: head) {{ {body} }}")


def test_an_offset_past_a_variable_run_is_refused() -> None:
	source = PREAMBLE + """
struct h { u8 n [max = 8]; u8 name[n]; u8 version [max = 3]; }
identify x(f: h) { required f.version; }
"""
	with pytest.raises(SituError, match="no fixed offset"):
		_formats(source)


def test_a_buffer_is_not_a_file() -> None:
	source = HEAD.replace("target file;", "target buffer;") + """
identify png(f: head) { sufficient f.signature; }
"""
	with pytest.raises(SituError, match="not `target file`"):
		_formats(source)


def test_the_png_example_is_recognised(tmp_path: Path) -> None:
	png  = tmp_path / "image.png"
	text = tmp_path / "notes.png"
	png.write_bytes(_chunk(b"IHDR"))
	text.write_bytes(b"not a png, whatever it is called")
	ran = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "identify", "-s",
		 str(ROOT / "example" / "png" / "png.situ"), str(png), str(text)],
		capture_output=True, text=True, timeout=120)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.splitlines() == [
		f"{png}: png (certain) image/png", f"{text}: - (no)"]


@pytest.mark.skipif(shutil.which("file") is None, reason="no file(1)")
def test_file_agrees_with_the_magic(tmp_path: Path) -> None:
	"""The rules file(1) reads must give its verdict on the same bytes:
	the IDAT head is refused by the required continuation, which a name
	on the first line would have printed anyway."""
	(tmp_path / "t.situ").write_text(GATED, encoding="latin-1")
	written = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "identify", "--magic",
		 "-s", str(tmp_path / "t.situ")],
		capture_output=True, text=True, timeout=120)
	assert written.returncode == 0, written.stderr
	(tmp_path / "t.magic").write_text(written.stdout, encoding="ascii")
	(tmp_path / "good").write_bytes(_chunk(b"IHDR"))
	(tmp_path / "deep").write_bytes(_chunk(b"IHDR", depth=200))
	(tmp_path / "idat").write_bytes(_chunk(b"IDAT"))
	said = {}
	for name in ("good", "deep", "idat"):
		ran = subprocess.run(
			["file", "-b", "-m", str(tmp_path / "t.magic"),
			 str(tmp_path / name)],
			capture_output=True, text=True, timeout=60)
		said[name] = ran.stdout.strip()
		ran = subprocess.run(
			["file", "-b", "--mime-type", "-m", str(tmp_path / "t.magic"),
			 str(tmp_path / name)],
			capture_output=True, text=True, timeout=60)
		said[name + " mime"] = ran.stdout.strip()
	assert said["good"] == "png", said
	assert said["good mime"] == "image/png", said
	assert said["deep"] != "png" and said["idat"] != "png", said


#: Four formats over overlapping bytes, so the tiebreak, every level and
#: every kind of test is reached: little-endian numbers, `!=`, ranges, a
#: closed enum, a byte-run enum's arm, an extension as proof.
MIXED = [
	GATED,
	HEAD + """
identify loose(f: head) { sufficient f.signature; }
""",
	# The same evidence again: an exact tie, which the earlier format wins
	# in both matchers. Without it no tie is ever reached, and a C
	# tiebreak that let the later one win passed this test.
	HEAD + """
identify twin(f: head) { sufficient f.signature; }
""",
	PREAMBLE.replace("endian big", "endian little") + """
enum mode : u16 { plain = 1, packed = 2, }
struct h { u8 magic[2] [must_eq = "MZ"]; u16 pages [min = 1, max = 64];
           mode how; u32 size; }
identify mz(f: h) [media = "application/x-dosexec"] {
	required   f.magic && f.how && f.pages;
	required   f.size != 0;
	supporting extension "exe", "com";
}
""",
	HEAD + """
identify raw(f: head) { sufficient extension "raw"; }
""",
	PREAMBLE + """
identify text [media = "text/plain"] { required text utf8; }
identify csv [media = "text/csv"] {
	required text csv; supporting extension "csv"; }
identify json [media = "application/json"] { required text json; }
""",
]

#: Text heads: valid and nearly valid UTF-8, CSV with quoted commas, JSON
#: behind a BOM, and a multi-byte sequence cut by a full window -- which
#: passes -- and by a short file's end, which does not. The last, lines
#: alike with no comma, is text and not a CSV of one column: without it a
#: C matcher that dropped the floor of one comma agreed with this suite.
TEXT_SEEDS = [b"a,b,c\n1,2,3\n", b'a,"b,c",d\n1,2,3\n', b"a,b\n1,2,3\n",
              b"\xef\xbb\xbf [1, 2]", b'{"k": "\xc3\xa9"}', b"plain\n",
              b"h\xc3\xa9llo", b"bad \xc3\x28", b"\xe0\x80\x80",
              b"\xed\xa0\x80", b"tab\there\x07", b"x" * 4095 + b"\xe2",
              b"x" * 4094 + b"\xe2\x82", b"x\xe2\x82", b"\xf4\x90\x80\x80",
              b"   ", b"a,b\n" * 1100, b"one\ntwo\nthree\n"]


def _mutations() -> list[tuple[bytes, str]]:
	"""Valid heads and their near misses -- a nearly right file reaches
	what a random one cannot -- under names that hit and miss."""
	import random
	chosen = random.Random(1124)
	seeds = [_chunk(b"IHDR"), _chunk(b"IDAT"), _chunk(b"IHDR", depth=17),
	         b"MZ\x02\x00\x01\x00\x10\x00\x00\x00",
	         b"MZ\x41\x00\x02\x00\x10\x00\x00\x00",
	         b"MZ\x02\x00\x03\x00\x00\x00\x00\x00", b"", b"MZ",
	         *TEXT_SEEDS]
	cases = []
	for seed in seeds:
		for name in ("a.png", "A.PNG", "b.exe", "c.raw", "d", "png", "e.csv"):
			cases.append((seed, name))
		for _ in range(40):
			data = bytearray(seed or b"\x00" * 12)
			for _ in range(chosen.randint(1, 3)):
				data[chosen.randrange(len(data))] = chosen.choice(
					[0, 1, 2, 3, 0x40, 0x41, 0xff, 0x2c, 0x0a, 0x22, 0x7b,
					 0xc3, 0x80, chosen.randrange(256)])
			cases.append((bytes(data[:chosen.randint(0, len(data))])
			              if chosen.random() < 0.2 else bytes(data),
			              chosen.choice(["x.png", "x.exe", "x.RAW", "x"])))
	return cases


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_the_c_matcher_agrees_with_the_python_one(tmp_path: Path) -> None:
	formats: list[identify.Recogniser] = []
	for source in MIXED:
		formats += _formats(source)
	(tmp_path / "id.c").write_text(identify.c_source(formats),
	                               encoding="ascii")
	(tmp_path / "drive.c").write_text(r"""#include <stdio.h>
#include <stdlib.h>
#include "id.c"
int main(void)
{
	static const char *const names[] = {"no", "possible", "likely", "certain"};
	static char line[20000];
	while (fgets(line, sizeof line, stdin) != NULL) {
		static uint8_t head[8192];
		size_t length = 0;
		char *name = strchr(line, ' ');
		const struct situ_id_format *chosen;
		int level;
		*name++ = '\0';
		name[strcspn(name, "\n")] = '\0';
		for (const char *at = line; at[0] && at[1]; at += 2) {
			unsigned byte;
			sscanf(at, "%2x", &byte);
			head[length++] = (uint8_t)byte;
		}
		level = situ_id_judge(head, length, name, &chosen);
		printf("%s %s\n", chosen ? chosen->name : "-", names[level]);
	}
	return 0;
}
""", encoding="ascii")
	built = subprocess.run(
		["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o",
		 str(tmp_path / "drive"), str(tmp_path / "drive.c")],
		capture_output=True, text=True, timeout=120)
	assert built.returncode == 0, built.stderr
	cases = _mutations()
	ran = subprocess.run(
		[str(tmp_path / "drive")], capture_output=True, text=True,
		timeout=60, input="".join(f"{head.hex() or '-'} {name}\n"
		                          for head, name in cases))
	assert ran.returncode == 0, ran.stderr
	expected = []
	for head, name in cases:
		chosen, level = identify.best(formats, head, name)
		expected.append(f"{chosen.name if chosen else '-'} {level}")
	said = ran.stdout.splitlines()
	assert len(said) == len(cases)
	wrong = [(head.hex(), name, mine, theirs) for (head, name), mine, theirs
	         in zip(cases, expected, said) if mine != theirs]
	assert not wrong, wrong[:5]
	# Every level was reached, and every format chosen, or the agreement
	# says less than it seems -- a sweep in which `twin` never lost a tie
	# or `mz` was never judged on its little-endian pages proves nothing
	# about either.
	assert {line.split()[1] for line in expected} == set(identify.LEVELS)
	assert {line.split()[0] for line in expected} == {
		"-", "png", "loose", "mz", "raw", "text", "csv", "json"}


@pytest.mark.skipif(shutil.which("update-mime-database") is None
                    or shutil.which("gio") is None,
                    reason="no shared-mime-info tools")
def test_the_desktop_agrees_with_the_mime_package(tmp_path: Path) -> None:
	"""The package goes through update-mime-database and gio, the real
	consumers: its first version opened with a comment holding `--`, which
	XML forbids, and only the real parser said so. The type is a private
	one, and the database the only one searched, so the system's own PNG
	cannot answer for it."""
	source = GATED.replace('media = "image/png"',
	                       'media = "application/x-situ-probe"').replace(
		"f.first.depth <= 16", "f.first.depth == 8")
	(tmp_path / "t.situ").write_text(source, encoding="latin-1")
	written = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situc"), "identify", "--mime",
		 "-s", str(tmp_path / "t.situ")],
		capture_output=True, text=True, timeout=120)
	assert written.returncode == 0, written.stderr
	packages = tmp_path / "mime" / "packages"
	packages.mkdir(parents=True)
	(packages / "situ.xml").write_text(written.stdout, encoding="utf-8")
	env = {"PATH": "/usr/bin:/bin", "XDG_DATA_HOME": str(tmp_path),
	       "XDG_DATA_DIRS": str(tmp_path), "HOME": str(tmp_path)}
	built = subprocess.run(["update-mime-database", str(tmp_path / "mime")],
	                       capture_output=True, text=True, timeout=120,
	                       env=env)
	assert "Failed to parse" not in built.stderr, built.stderr
	said = {}
	for name, data in (("good", _chunk(b"IHDR")),
	                   ("idat", _chunk(b"IDAT")),
	                   ("deep", _chunk(b"IHDR", depth=9))):
		(tmp_path / f"{name}.bin").write_bytes(data)
		ran = subprocess.run(
			["gio", "info", "-a", "standard::content-type",
			 str(tmp_path / f"{name}.bin")],
			capture_output=True, text=True, timeout=60, env=env)
		said[name] = ran.stdout.split("standard::content-type:")[-1].strip()
	assert said == {"good": "application/x-situ-probe",
	                "idat": "application/octet-stream",
	                "deep": "application/octet-stream"}, said


def _edit(*argv: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"), *argv],
		capture_output=True, text=True, timeout=300)


def test_the_editor_reads_a_file_as_the_format_it_is(tmp_path: Path) -> None:
	"""`situ-edit DIR FILE`: the detectors reused to choose the schema, as
	the holder asked of them -- the file read as the struct its format's
	`identify` reads, named on stderr with how sure."""
	schemas = tmp_path / "schemas"
	schemas.mkdir()
	(schemas / "head.situ").write_text(GATED, encoding="latin-1")
	(schemas / "text.situ").write_text(
		PREAMBLE + "identify csv { required text csv; }\n",
		encoding="ascii")
	(schemas / "plain.situ").write_text(HEAD, encoding="latin-1")
	png = tmp_path / "image.png"
	png.write_bytes(_chunk(b"IHDR"))
	ran = _edit(str(schemas), str(png))
	assert ran.returncode == 0, ran.stderr
	assert "is png (certain), read as head" in ran.stderr
	assert ran.stdout.startswith("head "), ran.stdout
	assert "signature" in ran.stdout and "first" in ran.stdout, ran.stdout

	other = tmp_path / "other.bin"
	other.write_bytes(b"\x00\x01\x02")
	ran = _edit(str(schemas), str(other))
	assert ran.returncode == 1
	assert "none of the 2 formats" in ran.stderr, ran.stderr

	table = tmp_path / "t.csv"
	table.write_bytes(b"a,b\n1,2\n")
	ran = _edit(str(schemas), str(table))
	assert ran.returncode == 1
	assert "a text format" in ran.stderr, ran.stderr
