"""The Qt window of decision 0034, held to the CLI.

0034's rule is that nothing the GUI can do is absent from the CLI, and the
window keeps it by construction -- it drives `situ-edit` and holds no editing
rule. What can still go wrong is the window's own half: a cell bound to the
wrong row, a byte run sent as a number, a refusal not shown. So these type
into the window's cells (`--no-ui` routes `--set` through the same handler a
person's edit runs) under Qt's offscreen platform, and compare the bytes it
writes with the CLI's.

Built once per session from `gui/`, into a temporary directory. Skipped only
where the toolchain is absent -- no `cmake`, or no Qt6 Widgets for it to
find -- and named as such; a build that fails with both present is a test
failure, not a skip.
"""

from __future__ import annotations

import json
import os
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


def _qt6_present() -> bool:
	"""Whether CMake would find Qt6 Widgets: its config file, where the
	distributions put it or under `CMAKE_PREFIX_PATH`. Not `cmake
	--find-package`, which is deprecated, misses config-file packages, and
	writes a `CMakeFiles/` into whatever directory it runs in -- the tree
	root, the first time this test ran."""
	roots = [Path(one) / "lib" for one in
	         os.environ.get("CMAKE_PREFIX_PATH", "").split(os.pathsep) if one]
	roots += [Path(one) for one in ("/usr/lib/x86_64-linux-gnu", "/usr/lib64",
	                                "/usr/lib", "/usr/lib/aarch64-linux-gnu")]
	return any((root / "cmake" / "Qt6Widgets" / "Qt6WidgetsConfig.cmake")
	           .is_file() for root in roots)


@pytest.fixture(scope="session")
def window_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
	if shutil.which("cmake") is None:
		pytest.skip("no cmake: the Qt window is not built here")
	if not _qt6_present():
		pytest.skip("no Qt6 Widgets: the Qt window is not built here")
	build = tmp_path_factory.mktemp("gui")
	configured = subprocess.run(
		["cmake", "-S", str(ROOT / "gui"), "-B", str(build)],
		capture_output=True, text=True, timeout=300)
	assert configured.returncode == 0, configured.stdout + configured.stderr
	built = subprocess.run(
		["cmake", "--build", str(build), "-j4"],
		capture_output=True, text=True, timeout=600)
	assert built.returncode == 0, built.stdout + built.stderr
	return build / "situ-edit-qt"


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
