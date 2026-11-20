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
QTTY = (ROOT / "vendor" / "qtty" / "qtty.pri").is_file()


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


@pytest.mark.skipif(not QTTY, reason="vendor/qtty is not checked out")
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
