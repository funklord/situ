"""The read-only editor of decision 0034.

Three frontends over one core, and the tests are mostly of the core because
that is where 0034 puts every rule. What is tested of the frontends is that
they cannot disagree: the TUI renders the same lines as the CLI because it
asks the same document, and the GUI -- when it exists -- must do the same.
"""

from __future__ import annotations

import ast as pyast
import subprocess
import sys
from pathlib import Path

import pytest

from editor.document import open_document
from editor.text import render
from every_schema import ROOT
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve
from walker.walk import Refused

SCHEMA = """target buffer;
endian big;

struct label {
	u16 id;
	u8  n;
	u8  name[n];
	u8  tail;
}
"""

MESSAGE = b"\x12\x34\x05hello\x7f"


def image(metadata: bool = True) -> bytes:
	schema   = parse_text(SCHEMA)
	resolved = resolve(schema, solve(schema))
	return pack(schema, resolved, metadata=metadata)[0]


# -- the core ---------------------------------------------------------------


def test_a_document_places_and_reads_every_member() -> None:
	document = open_document(image(), MESSAGE)

	rows = {f.name: f for f in document.fields()}

	assert rows["id"].offset == 0 and rows["id"].value == 0x1234
	assert rows["name"].offset == 3 and rows["name"].value == b"hello"
	assert rows["tail"].offset == 8 and rows["tail"].value == 0x7f


def test_the_document_outlives_the_buffer_it_was_read_from() -> None:
	"""What rung 2 buys, and the reason an editor can hold a document after
	closing the file it came from."""
	raw      = bytearray(MESSAGE)
	document = open_document(image(), raw)
	fields   = document.fields()

	raw[:] = b"\xAA" * len(raw)

	assert {f.name: f.value for f in fields}["name"] == b"hello"


def test_placement_and_value_are_asked_separately() -> None:
	"""A field the walk can locate but not read is a different thing from
	one it cannot locate, and an editor wants to show the first at its
	offset rather than drop it. Every member keeps a row either way.
	"""
	document = open_document(image(), MESSAGE)

	assert len(document.fields()) == 4
	assert all(f.readable or f.note for f in document.fields())


def test_an_unknown_struct_is_refused_by_name() -> None:
	with pytest.raises(Refused, match="no struct `nope`"):
		open_document(image(), MESSAGE, struct="nope")


def test_without_the_metadata_tail_the_fields_are_numbered() -> None:
	"""26.33 split names into the tail for a reader rather than a device.
	An editor is a reader, so its frontends ask for it -- but the core must
	still open an image that lacks one."""
	rows = open_document(image(metadata=False), MESSAGE).fields()

	assert all(f.name.startswith("placement[") for f in rows)


# -- the boundary 0034 rests on ---------------------------------------------


def test_the_editor_does_not_import_the_compiler() -> None:
	"""0026 keeps the compiler and the interpreter apart, and 0034 keeps
	that while still letting a user open a `.situ`: the editor reads images,
	and opening a schema runs `situc pack` in a *process*.

	Asserted from the source rather than from a successful import, because
	an import that happens not to have run yet proves nothing.
	"""
	for path in sorted((ROOT / "editor").glob("*.py")):
		tree = pyast.parse(path.read_text(encoding="ascii"))
		for node in pyast.walk(tree):
			if isinstance(node, pyast.Import):
				names = [alias.name for alias in node.names]
			elif isinstance(node, pyast.ImportFrom):
				names = [node.module or ""]
			else:
				continue
			for name in names:
				assert not name.startswith("situc"), \
					f"{path.name} imports {name}; 0026 keeps them apart"


# -- the frontends ----------------------------------------------------------


def run(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run([sys.executable, str(ROOT / "bin" / binary), *args],
	                      capture_output=True, text=True, cwd=ROOT)


def test_the_cli_reads_a_schema_by_running_the_packer(tmp_path: Path) -> None:
	"""The process boundary, exercised: a `.situ` in, fields out, and no
	compiler linked into the editor to do it."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)

	done = run("situ-edit", str(tmp_path / "s.situ"), str(tmp_path / "m.bin"))

	assert done.returncode == 0, done.stderr
	assert "label" in done.stdout
	assert "name" in done.stdout and "68656c6c6f" in done.stdout


def test_the_cli_reads_a_packed_image_with_no_compiler(tmp_path: Path) -> None:
	(tmp_path / "s.img").write_bytes(image())
	(tmp_path / "m.bin").write_bytes(MESSAGE)

	done = run("situ-edit", str(tmp_path / "s.img"), str(tmp_path / "m.bin"))

	assert done.returncode == 0, done.stderr
	assert "id" in done.stdout


def test_the_tui_shows_exactly_what_the_cli_shows(tmp_path: Path) -> None:
	"""0034's rule is that nothing the TUI can do is absent from the CLI.
	The strongest form of that is the same lines, which is what `--no-ui`
	exists to make checkable without a terminal.
	"""
	(tmp_path / "s.img").write_bytes(image())
	(tmp_path / "m.bin").write_bytes(MESSAGE)

	cli = run("situ-edit", str(tmp_path / "s.img"), str(tmp_path / "m.bin"))
	tui = run("situ-edit-tui", "--no-ui",
	          str(tmp_path / "s.img"), str(tmp_path / "m.bin"))

	assert cli.returncode == 0 and tui.returncode == 0, tui.stderr
	assert cli.stdout == tui.stdout


def test_the_cli_takes_hex(tmp_path: Path) -> None:
	(tmp_path / "s.img").write_bytes(image())
	(tmp_path / "m.hex").write_text("1234 05 68 65 6c 6c 6f 7f", encoding="ascii")

	done = run("situ-edit", "--hex",
	           str(tmp_path / "s.img"), str(tmp_path / "m.hex"))

	assert done.returncode == 0, done.stderr
	assert "68656c6c6f" in done.stdout


def test_render_keeps_a_row_for_a_field_it_could_not_read() -> None:
	"""Dropping it would show a message missing something it has."""
	document = open_document(image(), MESSAGE)
	lines    = render(document)

	assert len(lines) == 1 + len(document.fields())


def test_the_readme_editor_sample_is_what_situ_edit_prints(tmp_path: Path) -> None:
	"""The README shows `situ-edit` reading a UDP capture. It is program
	output pasted into prose, and a stale copy cannot be told from a current
	one by reading it -- the same ground as the `advise`, `map`, `doc` and
	`explain` samples (26.166, 26.168). This block elides nothing, so it is
	held whole.

	The capture is eight bytes and the README names the values it holds, so
	it is rebuilt here rather than committed: a fixture that has to agree
	with a document is one more copy to drift.
	"""
	from test_cli import flat, readme_block

	capture = tmp_path / "capture.bin"
	capture.write_bytes(bytes.fromhex("12340035" "0008abcd"))

	done = run("situ-edit", str(ROOT / "example/udp/udp.situ"), str(capture))
	assert done.returncode == 0, done.stderr

	printed = [flat(line) for line in done.stdout.splitlines() if line.strip()]
	block   = readme_block("$ situ-edit example/udp/udp.situ capture.bin")
	shown   = [flat(line) for line in block
	           if line.strip() and not line.startswith("$ ")]

	assert printed[:len(shown)] == shown
