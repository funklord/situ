"""The window's cell editors refuse at the key what the core would refuse.

26.1114 gave each value cell the widget its field wants -- a searchable
combo for an enum, a number field, a hex field no longer than its run --
and Qt is full of quirks in exactly that: an editable combo ate Return, so
a choice sat in an open editor until focus happened to leave, and closing
that editor as the window was destroyed committed through a session that no
longer existed (a segfault). Both were found by these cases, not by
reading.

So each case presses real keys into the real editor (`--keys`), and runs
twice: through Qt's own delivery -- the active popup first, else the focus
widget -- and through qtty's `InputRouter`, which is how a terminal's bytes
arrive (`--tui`). qtty is new, and a constraint that holds in the window
and not on the terminal is the failure this exists to see. The expected
values are worked from the schema, not taken from either run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
from test_the_qt_window_edits_as_the_cli_does import QTTY, window_binary

__all__ = ["window_binary"]		# the fixture, re-exported for pytest

SCHEMA = """target buffer;
endian big;

enum mode : u8 { client = 3, server = 4, broadcast = 5 }
enum flavour : u8 { plain = 0, salted = 1, default = pass }
struct pick { mode m; flavour f; u8 other; u8 tag[2]; u8 n; u8 note[n]; }
"""

#: m = server, f = 7 (no member names it; the enum admits it), other = 1,
#: tag = aa bb, n = 1, note = cc.
MESSAGE = bytes([4, 7, 1, 0xAA, 0xBB, 1, 0xCC])

#: (keys, field, what the row reads after, what the editor still holds or
#: None where Return closed it).
CASES = [
	# A closed enum: nothing outside its members can be typed, and the
	# completer finds a member by any part of its name.
	("m=zz{Return}", "m", "server (4)", None),
	("m=broad{Down}{Return}", "m", "broadcast (5)", None),
	("m=cast{Down}{Return}", "m", "broadcast (5)", None),
	("m=client{Return}", "m", "client (3)", None),
	("m=client (3){Return}", "m", "client (3)", None),
	# Half a name is not a member: Return leaves it open, uncommitted.
	("m=serv{Return}", "m", "server (4)", "serv"),
	# An open enum takes a member by name, or a number it does not name.
	("f=salted{Return}", "f", "salted (1)", None),
	("f=zz9{Return}", "f", "9", None),
	# A number: no letters but a 0x prefix's hex, and `0x` alone is not one.
	("other=12a3{Return}", "other", "123", None),
	("other=0x2a{Return}", "other", "42", None),
	("other=0x{Return}", "other", "1", "0x"),	# a prefix, not a number
	# ...and inside the field's range, which is the walker's own write
	# rule (`scalar_range`): a u8 takes no sign and nothing past 255, so
	# the digit that would pass it is refused at the key.
	("other=300{Return}", "other", "30", None),
	("other=-5{Return}", "other", "5", None),
	("other=0x1ff{Return}", "other", "31", None),
	# A run takes hex digits, and no more of them than its two bytes hold.
	("tag=zz12345{Return}", "tag", "1234", None),
	# ...unless it may change length (26.1115): `note` is sized by `n`,
	# which the core moves with it, so three bytes go in where one was.
	("note=zzccddee{Return}", "note", "ccddee", None),
]

DELIVERIES = ["qt", pytest.param("qtty", marks=pytest.mark.skipif(
	not QTTY, reason="vendor/qtty is not checked out"))]


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	where = tmp_path_factory.mktemp("constrained")
	(where / "k.situ").write_text(SCHEMA, encoding="ascii")
	(where / "k.bin").write_bytes(MESSAGE)
	return where / "k.situ", where / "k.bin"


@pytest.mark.parametrize("delivery", DELIVERIES)
@pytest.mark.parametrize("keys,field,after,held", CASES)
def test_a_cell_takes_only_what_its_field_can_hold(
		window_binary: Path, files: tuple[Path, Path], delivery: str,
		keys: str, field: str, after: str, held: str | None) -> None:
	schema, message = files
	environment = dict(os.environ, SITU_BIN=str(ROOT / "bin"))
	extra = ["--tui"] if delivery == "qtty" else []
	if delivery == "qt":
		environment["QT_QPA_PLATFORM"] = "offscreen"
	done = subprocess.run(
		[str(window_binary), "--no-ui", *extra, "--keys", keys,
		 str(schema), str(message)],
		capture_output=True, text=True, timeout=120, env=environment,
		cwd=ROOT, stdin=subprocess.DEVNULL)
	assert done.returncode == 0, done.stderr

	rows = {line.split("\t")[2]: line.split("\t")[3]
	        for line in done.stdout.splitlines() if line.count("\t") >= 3}
	assert rows[field] == after, done.stdout
	open_with = [line[len("editor: "):] for line in done.stdout.splitlines()
	             if line.startswith("editor: ")]
	assert open_with == ([] if held is None else [held]), done.stdout
	# The message opened is never what the window writes to.
	assert message.read_bytes() == MESSAGE


@pytest.mark.skipif(not QTTY, reason="vendor/qtty is not checked out")
def test_the_search_list_is_drawn_on_a_terminal(
		window_binary: Path, files: tuple[Path, Path]) -> None:
	"""The enum's completer list, open over the window, in the frame qtty
	composes. Until qtty a901586 it was routed and never drawn -- a person
	searched blind -- which every case above missed, since they check what
	the keys do and not what the screen shows. And it is as wide as the
	member it offers, not as the cell (`broadc...` in a narrow column)."""
	schema, message = files
	shown = subprocess.run(
		[str(window_binary), "--tui", "--keys", "m=cast", "--snapshot",
		 "90x12", str(schema), str(message)],
		capture_output=True, text=True, timeout=120, cwd=ROOT,
		env=dict(os.environ, SITU_BIN=str(ROOT / "bin")),
		stdin=subprocess.DEVNULL)
	assert shown.returncode == 0, shown.stderr
	screen = shown.stdout.split("--- attrs ---")[0]
	assert "broadcast (5)" in screen, screen
