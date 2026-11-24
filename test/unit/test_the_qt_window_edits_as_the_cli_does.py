"""The Qt window of decision 0034, held to the CLI.

0034's rule is that nothing the GUI can do is absent from the CLI, and the
window keeps it by construction -- it drives `situ-edit` and holds no editing
rule. What can still go wrong is the window's own half: a cell bound to the
wrong row, a byte run sent as a number, a refusal not shown. So these type
into the window's cells (`--no-ui` routes `--set` through the same handler a
person's edit runs) under Qt's offscreen platform, and compare the bytes it
writes with the CLI's.

Built once per session by `make gui`, into a temporary directory. Skipped
only where the toolchain is absent -- no qmake for Qt 6 Widgets -- and named
as such; a build that fails with it present is a test failure, not a skip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

SCHEMA = """target buffer;
endian big;

struct label {
	u16 id;
	u8  n;
	u8  name[n];
	u8  tail [max = 0x7f];
}
"""

MESSAGE = b"\x12\x34\x05hello\x7f"


def _qmake() -> str | None:
	"""qmake for Qt 6 with its Widgets headers, or None: what `make gui`
	needs. Asked of qmake itself rather than guessed from paths."""
	for name in ("qmake6", "qmake"):
		found = shutil.which(name)
		if found is None:
			continue
		asked = subprocess.run([found, "-query", "QT_VERSION"],
		                       capture_output=True, text=True, timeout=60)
		headers = subprocess.run([found, "-query", "QT_INSTALL_HEADERS"],
		                         capture_output=True, text=True, timeout=60)
		if asked.stdout.startswith("6.") and \
				(Path(headers.stdout.strip()) / "QtWidgets").is_dir():
			return found
	return None


#: Whether the qtty half can be built: its submodule checked out. A tree
#: without it builds the window alone, and the terminal tests skip saying so.
QTTY = (ROOT / "qtty" / "qtty.pri").is_file()


@pytest.fixture(scope="session")
def window_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
	"""`make gui` into a build directory of the test's own, so the binary
	is the one these sources build and not whatever the tree last made."""
	qmake = _qmake()
	if qmake is None:
		pytest.skip("no qmake for Qt 6 Widgets: the window is not built here")
	build = tmp_path_factory.mktemp("gui")
	built = subprocess.run(
		["make", "-C", str(ROOT), "gui", "-j4", f"BUILD_DIR={build}",
		 f"QMAKE={qmake}"],
		capture_output=True, text=True, timeout=1200)
	assert built.returncode == 0, built.stdout + built.stderr
	return build / "situ-edit-gui"


def _window(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
	environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
	                   SITU_BIN=str(ROOT / "bin"))
	return subprocess.run([str(binary), "--no-ui", *args],
	                      capture_output=True, text=True, timeout=120,
	                      env=environment, cwd=ROOT)


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, str(ROOT / "bin" / "situ-edit"),
	                       *args], capture_output=True, text=True,
	                      timeout=120, cwd=ROOT)


def test_the_window_writes_exactly_what_the_cli_writes(
		window_binary: Path, tmp_path: Path) -> None:
	"""A number and a byte run, typed into their cells: `name` is a middle
	row and takes hex, so a cell bound to the wrong row or a run sent as a
	number cannot produce these bytes."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)
	edits = ("--set", "id=0x4242", "--set-bytes", "name=776f726c64")

	window = _window(window_binary, *edits, "--out", str(tmp_path / "q.bin"),
	                 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	cli = _cli(*edits, "--out", str(tmp_path / "c.bin"),
	           str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))

	assert window.returncode == 0, window.stderr
	assert cli.returncode == 0, cli.stderr
	written = (tmp_path / "q.bin").read_bytes()
	assert written == (tmp_path / "c.bin").read_bytes()
	assert written == b"\x42\x42\x05world\x7f"
	assert (tmp_path / "m.bin").read_bytes() == MESSAGE
	# The table shows what the bytes now hold, row by row.
	rows = {line.split("\t")[2]: line.split("\t")[3]
	        for line in window.stdout.splitlines() if line.count("\t") >= 3}
	assert rows["id"] == str(0x4242) and rows["name"] == "776f726c64"


def test_the_window_refuses_what_the_cli_refuses(
		window_binary: Path, tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)

	window = _window(window_binary, "--set", "n=999",
	                 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	cli = _cli("--set", "n=999",
	           str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))

	assert window.returncode == 1 and cli.returncode == 1
	assert cli.stderr.strip().split(": ", 1)[1] in window.stderr


def test_the_window_shows_the_verdict(
		window_binary: Path, tmp_path: Path) -> None:
	"""`tail [max = 0x7f]` holding 0x80: the header carries the verdict the
	CLI's first line does, from the JSON's `verdict` -- which the JSON did
	not have until the window needed it."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE[:-1] + b"\x80")

	window = _window(window_binary,
	                 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	cli = _cli(str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))

	assert window.returncode == 0, window.stderr
	word = cli.stdout.splitlines()[0].rsplit("[", 1)[1].rstrip("]")
	assert word != "ok"
	assert f"[{word}]" in window.stdout.splitlines()[0]


NESTED = """target buffer;
endian big;

struct inner { u8 a; u16 b; }
struct outer { u8 tag; inner in; u8 tail; }
"""


def test_a_nested_struct_opens_in_its_own_window_over_the_same_bytes(
		window_binary: Path, tmp_path: Path) -> None:
	"""`--open in` is the double click on the nested row: a second window
	over the SAME working bytes, so its edit lands in the whole buffer --
	what the CLI does with `--struct inner --offset 1`, taken from the
	JSON's `nested` rather than retyped from the note."""
	(tmp_path / "s.situ").write_text(NESTED, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(b"\x01\x02\x00\x03\x04")

	window = _window(window_binary, "--struct", "outer",
	                 "--open", "in", "--set", "b=0x1234",
	                 "--out", str(tmp_path / "q.bin"),
	                 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	cli = _cli("--struct", "inner", "--offset", "1", "--length", "3",
	           "--set", "b=0x1234", "--out", str(tmp_path / "c.bin"),
	           str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))

	assert window.returncode == 0, window.stderr
	assert cli.returncode == 0, cli.stderr
	assert window.stdout.splitlines()[0].startswith("inner")
	assert (tmp_path / "q.bin").read_bytes() == b"\x01\x02\x12\x34\x04"
	assert (tmp_path / "q.bin").read_bytes() \
		== (tmp_path / "c.bin").read_bytes()


def test_the_json_names_a_nested_struct_as_data(tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(NESTED, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(b"\x01\x02\x00\x03\x04")
	done = _cli("--format", "json", "--struct", "outer",
	            str(tmp_path / "s.situ"),
	            str(tmp_path / "m.bin"))
	assert done.returncode == 0, done.stderr
	fields = {one["name"]: one for one in json.loads(done.stdout)["fields"]}
	assert fields["in"]["nested"] == {"struct": "inner", "offset": 1,
	                                  "length": 3}
	# One struct, not a run of them (26.1144).
	assert fields["in"]["elements"] is None
	assert fields["tag"]["nested"] is None


NTP = ROOT / "example" / "ntp"


def _ntp_reply(tmp_path: Path) -> Path:
	"""NTP's server reply from the golden vectors, as `make gui-example`
	takes it."""
	for line in (NTP / "ntp.vectors").read_text(encoding="ascii").splitlines():
		words = line.split()
		if words[:2] == ["ntp_packet", "server_reply"]:
			(tmp_path / "reply.hex").write_text(" ".join(words[2:]),
			                                    encoding="ascii")
			return tmp_path / "reply.hex"
	raise AssertionError("ntp.vectors has no server_reply")


@pytest.mark.skipif(not QTTY, reason="qtty is not checked out")
def test_the_window_on_a_terminal_shows_the_cli_rows(
		window_binary: Path, tmp_path: Path) -> None:
	"""The same window drawn by qtty (`--tui`), every row of it the CLI's:
	each field's name and value, from the JSON's `view`, on one line of the
	terminal. A cell bound to the wrong row puts a value beside another
	field's name, which this sees."""
	reply = _ntp_reply(tmp_path)
	shown = subprocess.run(
		[str(window_binary), "--tui", "--snapshot", "140x30", "--hex",
		 "--struct", "ntp_packet", str(NTP / "ntp.situ"), str(reply)],
		capture_output=True, text=True, timeout=120, cwd=ROOT,
		env=dict(os.environ, SITU_BIN=str(ROOT / "bin")),
		stdin=subprocess.DEVNULL)
	assert shown.returncode == 0, shown.stderr
	screen = shown.stdout.split("--- attrs ---")[0]
	view = json.loads(_cli("--format", "json", "--hex", "--struct",
	                       "ntp_packet", str(NTP / "ntp.situ"),
	                       str(reply)).stdout)["view"]
	assert view["heading"][0] in screen
	cells = {}
	for line in screen.splitlines():
		parts = [part.strip() for part in re.split("[\u2502|]", line)]
		if len(parts) > 4:
			cells[parts[3]] = parts[4]
	assert len(view["rows"]) == 13
	for row in view["rows"]:
		assert cells.get(row["name"]) == row["value"], (row, screen)


def test_a_typed_value_is_put_as_the_field_shows_it(tmp_path: Path) -> None:
	"""`--put`, what both interactive frontends send for a typed cell: hex
	for a byte run, a number otherwise, chosen by the core -- and a name no
	field has refused in the core's words, not as "not a number"."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)
	done = _cli("--put", "id=0x4242", "--put", "name=776f726c64",
	            "--out", str(tmp_path / "o.bin"),
	            str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	assert done.returncode == 0, done.stderr
	assert (tmp_path / "o.bin").read_bytes() == b"\x42\x42\x05world\x7f"
	refused = _cli("--put", "nosuch=zz",
	               str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	assert refused.returncode == 1
	assert "no member named `nosuch`" in refused.stderr


def test_a_missing_message_is_a_sentence(tmp_path: Path) -> None:
	"""Not a traceback: the window shows the CLI's stderr in its status."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	done = _cli(str(tmp_path / "s.situ"), str(tmp_path / "absent.bin"))
	assert done.returncode == 1
	assert done.stderr.strip() == (f"cannot read {tmp_path / 'absent.bin'}: "
	                               "No such file or directory")


def test_the_window_says_who_wrote_it(window_binary: Path) -> None:
	"""`--version`: the version on the first line, which a script parses,
	and the copyright line situc prints on a line of its own."""
	from situc.cli import COPYRIGHT

	done = subprocess.run([str(window_binary), "--version"],
	                      capture_output=True, text=True, timeout=60)
	assert done.returncode == 0, done.stderr
	version = (ROOT / "VERSION").read_text(encoding="ascii").strip()
	assert done.stdout.splitlines() == [f"situ-edit-gui {version}", COPYRIGHT]


@pytest.mark.parametrize("desktop,path,text,want", [
	("TDE", ".trinity/share/config/kdeglobals",
	 "[General]\nwindowBackground=36,36,36\nwindowForeground=220,220,220\n",
	 "dark"),
	("TDE", ".trinity/share/config/kdeglobals",
	 "[General]\nwindowBackground=239,239,239\nwindowForeground=0,0,0\n",
	 "light"),
	("LXQt", ".config/lxqt/lxqt.conf",
	 "[Palette]\nwindow_color=#232323\nwindow_text_color=#e0e0e0\n", "dark"),
])
def test_the_window_follows_a_dark_desktop(window_binary: Path,
		tmp_path: Path, desktop: str, path: str, text: str,
		want: str) -> None:
	"""harmonization.md's GUI list: a dark palette on a dark desktop. On TDE
	Qt alone hands a program a light palette, so the desktop's own file is
	asked before the palette is (qtkit/theme.h); `--scheme` says what
	the window follows. The files are this machine's measured dialects."""
	(tmp_path / path).parent.mkdir(parents=True)
	(tmp_path / path).write_text(text, encoding="ascii")
	environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
	               "TDEHOME": str(tmp_path / ".trinity"),
	               "XDG_CONFIG_HOME": str(tmp_path / ".config"),
	               "XDG_CURRENT_DESKTOP": desktop,
	               "QT_QPA_PLATFORM": "offscreen", "LANG": "C.UTF-8"}
	done = subprocess.run([str(window_binary), "--scheme"],
	                      capture_output=True, text=True, timeout=60,
	                      env=environment)
	assert done.stdout.strip() == want, done.stderr
	environment["SITU_THEME"] = "light" if want == "dark" else "dark"
	overridden = subprocess.run([str(window_binary), "--scheme"],
	                            capture_output=True, text=True, timeout=60,
	                            env=environment)
	assert overridden.stdout.strip() == environment["SITU_THEME"]


GROWN = """target buffer;
endian big;
struct inner { u8 n; u8 name[n]; u8 flag; }
struct outer { u8 kind; inner body; u8 end; }
"""


def test_a_run_grows_in_a_nested_window(window_binary: Path,
		tmp_path: Path) -> None:
	"""A byte run written longer in a nested window: the window names the
	window it was opened inside (`--within`), so the CLI can move `n` and
	what follows, and the window follows its own frame afterwards -- the
	second write lands where the first left the bytes (26.1141)."""
	(tmp_path / "s.situ").write_text(GROWN, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(bytes.fromhex("07026869" "01ee"))

	window = _window(window_binary, "--struct", "outer", "--open", "body",
	                 "--set-bytes", "name=68656c6c6f",
	                 "--set-bytes", "name=787978",
	                 "--out", str(tmp_path / "q.bin"),
	                 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))
	assert window.returncode == 0, window.stderr
	assert (tmp_path / "q.bin").read_bytes() \
		== bytes.fromhex("0703" "787978" "01ee")


def test_an_element_of_a_run_opens_and_grows(window_binary: Path,
		tmp_path: Path) -> None:
	"""`--open chunks:1` is the second chunk of a PNG, which double-clicking
	`chunks` offers by where each element is: opening the run whole read
	IHDR alone (26.1144). Its text grown there moves the chunk's length
	and CRC, as the CLI does with `--within` (26.1141)."""
	import struct
	import zlib

	def chunk(kind: bytes, data: bytes) -> bytes:
		return struct.pack(">I", len(data)) + kind + data \
			+ struct.pack(">I", zlib.crc32(kind + data))

	def png(text: bytes) -> bytes:
		return bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", bytes(13)) \
			+ chunk(b"tEXt", text) + chunk(b"IEND", b"")

	(tmp_path / "f.png").write_bytes(png(b"Comment\x00hi"))
	window = _window(window_binary, "--struct", "png_file",
	                 "--open", "chunks:1",
	                 "--set-bytes", "data=" + b"Comment\x00hello".hex(),
	                 "--out", str(tmp_path / "g.png"),
	                 str(ROOT / "example" / "png" / "png.situ"),
	                 str(tmp_path / "f.png"))
	assert window.returncode == 0, window.stderr
	assert (tmp_path / "g.png").read_bytes() == png(b"Comment\x00hello")


@pytest.mark.parametrize("first", [
	# The window over the chunk AFTER the one grown: it moves by as much.
	"chunks:2",
	# A second window over the very chunk grown: its frame grows with it.
	"chunks:1",
])
def test_a_window_follows_a_resize_made_in_another(window_binary: Path,
		tmp_path: Path, first: str) -> None:
	"""Two windows over a PNG's chunks, as a person leaves them open: text
	grown in one moves the bytes the other stands on. That window was
	refused until reopened (26.1145); it now moves its frame by the write
	and its own write lands in its own chunk (26.1146)."""
	import struct
	import zlib

	def chunk(kind: bytes, data: bytes) -> bytes:
		return struct.pack(">I", len(data)) + kind + data \
			+ struct.pack(">I", zlib.crc32(kind + data))

	def png(*texts: bytes) -> bytes:
		return bytes.fromhex("89504e470d0a1a0a") + chunk(b"IHDR", bytes(13)) \
			+ b"".join(chunk(b"tEXt", text) for text in texts) \
			+ chunk(b"IEND", b"")

	(tmp_path / "f.png").write_bytes(png(b"A\x00one", b"B\x00two"))
	window = _window(window_binary, "--struct", "png_file",
	                 "--open", first, "--open", "/chunks:1",
	                 "--set-bytes", "@2:data=" + b"A\x00eleven".hex(),
	                 "--set-bytes", "@1:data=" + b"Z\x00twelve".hex(),
	                 "--out", str(tmp_path / "g.png"),
	                 str(ROOT / "example" / "png" / "png.situ"),
	                 str(tmp_path / "f.png"))
	assert window.returncode == 0, window.stderr
	want = (png(b"A\x00eleven", b"Z\x00twelve") if first == "chunks:2"
	        else png(b"Z\x00twelve", b"B\x00two"))
	assert (tmp_path / "g.png").read_bytes() == want


def _png_chunk(kind: bytes, data: bytes, crc: int | None = None) -> bytes:
	import struct
	import zlib
	return struct.pack(">I", len(data)) + kind + data + struct.pack(
		">I", zlib.crc32(kind + data) if crc is None else crc)


def _png_of(*middle: bytes) -> bytes:
	return bytes.fromhex("89504e470d0a1a0a") \
		+ _png_chunk(b"IHDR", bytes(13)) + b"".join(middle) \
		+ _png_chunk(b"IEND", b"")


def test_the_window_inserts_and_removes_an_element(window_binary: Path,
		tmp_path: Path) -> None:
	"""Edit > Insert Element writes what `--insert` writes, its CRC computed
	(26.1147); a window open on the chunk after the insert follows it, so
	its write lands in its own chunk, now one further on."""
	(tmp_path / "f.png").write_bytes(_png_of(_png_chunk(b"tEXt", b"A\x00a")))
	window = _window(window_binary, "--struct", "png_file",
	                 "--open", "chunks:1",
	                 "--insert", "@0:chunks:1="
	                 + _png_chunk(b"tEXt", b"N\x00new", crc=0).hex(),
	                 "--set-bytes", "@1:data=" + b"A\x00again".hex(),
	                 "--out", str(tmp_path / "g.png"),
	                 str(ROOT / "example" / "png" / "png.situ"),
	                 str(tmp_path / "f.png"))
	assert window.returncode == 0, window.stderr
	assert (tmp_path / "g.png").read_bytes() == _png_of(
		_png_chunk(b"tEXt", b"N\x00new"), _png_chunk(b"tEXt", b"A\x00again"))

	window = _window(window_binary, "--struct", "png_file",
	                 "--open", "chunks:2", "--remove", "@0:chunks:1",
	                 "--set-bytes", "@1:data=" + b"B\x00b".hex(),
	                 "--out", str(tmp_path / "h.png"),
	                 str(ROOT / "example" / "png" / "png.situ"),
	                 str(tmp_path / "g.png"))
	assert window.returncode == 0, window.stderr
	assert (tmp_path / "h.png").read_bytes() \
		== _png_of(_png_chunk(b"tEXt", b"B\x00b"))


def test_a_window_on_a_removed_element_refuses(window_binary: Path,
		tmp_path: Path) -> None:
	"""The chunk a window stood on is gone: nothing it could write to is
	its own, so it refuses until reopened rather than writing into the
	chunk that moved up into its place (26.1146, 26.1147)."""
	(tmp_path / "f.png").write_bytes(_png_of(_png_chunk(b"tEXt", b"A\x00a"),
	                                         _png_chunk(b"tEXt", b"B\x00b")))
	window = _window(window_binary, "--struct", "png_file",
	                 "--open", "chunks:1", "--remove", "@0:chunks:1",
	                 "--set-bytes", "@1:data=" + b"C\x00c".hex(),
	                 str(ROOT / "example" / "png" / "png.situ"),
	                 str(tmp_path / "f.png"))
	assert window.returncode != 0
	assert "reopen it" in window.stderr


def test_the_window_has_an_icon_with_no_theme(window_binary: Path,
		tmp_path: Path) -> None:
	"""The window icon falls back to the SVG the package installs, compiled
	in (gui.qrc): under -platform offscreen, or run from the tree, the
	theme holds no `se.vibes.situ-edit-gui`, and the window had none
	(qt-desktop-integration skill, claude-guidelines inbox 20261124T2111Z)."""
	environment = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
	               "QT_QPA_PLATFORM": "offscreen", "LANG": "C.UTF-8"}
	done = subprocess.run([str(window_binary), "--window-icon"],
	                      capture_output=True, text=True, timeout=60,
	                      env=environment)
	assert done.stdout.strip() == "present", done.stderr
