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


# -- what a write would cost (26.177) ---------------------------------------


def test_a_field_says_whether_the_schema_lets_anyone_write_it() -> None:
	"""`readable` had no mirror.

	The image has carried the capability vectors since 26.33 split the tail
	off for this reader, and `situ-edit` asks for them with `--metadata` --
	and nothing read them, so an editor could show a field and not say
	whether writing it was legal. That is the one question a *file* editor
	has to answer before touching a byte.

	`example/udp` is the case that exercises every answer: three covered
	fields a write may touch in place, a checksum nobody may write, and a
	payload whose write moves what follows it.
	"""
	schema  = (ROOT / "example/udp/udp.situ").read_text(encoding="ascii")
	parsed  = parse_text(schema, path="udp.situ")
	image, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)

	document = open_document(image, bytearray(bytes.fromhex("123400350008abcd")))
	fields   = {field.name: field for field in document.fields()}

	assert fields["source_port"].writable
	assert fields["source_port"].mutate == "InPlaceFixed"
	assert fields["source_port"].auth == "Covered"
	assert "a tag has to be recomputed" in fields["source_port"].write_cost

	assert not fields["checksum"].writable
	assert "the schema does not let anyone write this" in \
		fields["checksum"].write_cost

	assert fields["payload"].writable
	assert "the bytes after it move" in fields["payload"].write_cost


def test_an_image_without_the_tail_says_it_was_not_told() -> None:
	"""Silence is not permission.

	An image packed without `--metadata` carries no vectors, and an editor
	that read the absence as "yes" would offer a write the schema forbids.
	`mutate` is `None` there, and `writable` is false.
	"""
	schema  = (ROOT / "example/udp/udp.situ").read_text(encoding="ascii")
	parsed  = parse_text(schema, path="udp.situ")
	image, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=False)

	document = open_document(image, bytearray(bytes.fromhex("123400350008abcd")))
	for field in document.fields():
		assert field.mutate is None
		assert not field.writable
		assert field.write_cost == "the image does not say"


# -- the write path (26.179) ------------------------------------------------


def udp_document() -> tuple[bytes, bytearray]:
	schema  = (ROOT / "example/udp/udp.situ").read_text(encoding="ascii")
	parsed  = parse_text(schema, path="udp.situ")
	image, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)
	return image, bytearray(bytes.fromhex("123400350008abcd"))


def test_a_permitted_write_lands_and_says_what_it_cost() -> None:
	image, message = udp_document()
	document = open_document(image, message)

	notes = document.set("destination_port", 4242)

	assert document.buffer.hex() == "123410920008abcd"
	assert notes and "stale" in notes[0], notes
	# situ does not compute a checksum (14.1), so it reports rather than
	# recomputes -- and the tag's bytes are untouched.
	assert document.buffer[6:8] == bytes.fromhex("abcd")


def test_the_document_edits_a_copy_and_not_the_caller_s_bytes() -> None:
	"""An edit that is never saved changes nothing anywhere."""
	image, message = udp_document()
	before = bytes(message)

	open_document(image, message).set("destination_port", 4242)

	assert bytes(message) == before


def test_the_three_refusals_are_three_different_things() -> None:
	"""Permission, existence and range are separate questions, and running
	them together would either refuse a legal write or allow an illegal
	one."""
	image, message = udp_document()
	document = open_document(image, message)

	with pytest.raises(Refused) as forbidden:
		document.set("checksum", 1)
	assert "does not let anyone write this" in str(forbidden.value)

	with pytest.raises(Refused) as absent:
		document.set("nope", 1)
	assert "no member named" in str(absent.value)

	with pytest.raises(Refused) as too_big:
		document.set("length", 70000)
	assert "does not fit a 16-bit unsigned member" in str(too_big.value)

	# And none of them moved a byte.
	assert document.buffer.hex() == "123400350008abcd"


def test_an_image_without_the_tail_says_which_half_is_missing() -> None:
	"""Without the tail there are no names either, so the lookup fails before
	the capability check does -- and "no member named `destination_port`"
	would be a lie about a member that is right there."""
	schema  = (ROOT / "example/udp/udp.situ").read_text(encoding="ascii")
	parsed  = parse_text(schema, path="udp.situ")
	image, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=False)

	document = open_document(image, bytearray(bytes.fromhex("123400350008abcd")))
	with pytest.raises(Refused) as why:
		document.set("destination_port", 1)
	assert "without its metadata tail" in str(why.value)


def test_nothing_reaches_the_file_without_out(tmp_path: Path) -> None:
	"""The safe default for a tool that edits files in place."""
	(tmp_path / "m.bin").write_bytes(bytes.fromhex("123400350008abcd"))

	done = run("situ-edit", str(ROOT / "example/udp/udp.situ"),
	           str(tmp_path / "m.bin"), "--set", "destination_port=4242")
	assert done.returncode == 0, done.stderr
	assert "4242" in done.stdout
	assert "not written" in done.stderr
	assert (tmp_path / "m.bin").read_bytes() == bytes.fromhex("123400350008abcd")

	done = run("situ-edit", str(ROOT / "example/udp/udp.situ"),
	           str(tmp_path / "m.bin"), "--set", "destination_port=4242",
	           "--out", str(tmp_path / "out.bin"))
	assert done.returncode == 0, done.stderr
	assert (tmp_path / "out.bin").read_bytes() == bytes.fromhex("123410920008abcd")
	assert (tmp_path / "m.bin").read_bytes() == bytes.fromhex("123400350008abcd")


def test_a_refused_write_exits_nonzero_and_writes_nothing(tmp_path: Path) -> None:
	(tmp_path / "m.bin").write_bytes(bytes.fromhex("123400350008abcd"))

	done = run("situ-edit", str(ROOT / "example/udp/udp.situ"),
	           str(tmp_path / "m.bin"), "--set", "checksum=1",
	           "--out", str(tmp_path / "out.bin"))

	assert done.returncode == 1
	assert "does not let anyone write this" in done.stderr
	assert not (tmp_path / "out.bin").exists()


def test_a_write_that_shifts_the_layout_is_refused() -> None:
	"""`InPlaceFixed` is about the member, not about its consequences.

	udp's `length` is a fixed scalar written in place, and it decides how
	long the payload is: storing 40 in an eight-byte message leaves it
	claiming a 32-byte payload, and nothing after the write can be read.
	0034's table has that as its second row -- "anything that shifts layout"
	-- and puts it behind 26.99. It arrived through the first row's door.

	Measured rather than analysed: the write goes into a copy, the copy is
	walked again, and every member's offset and size are compared. That
	catches the case whatever caused it, and it is the walk answering rather
	than a second model of what drives what.
	"""
	image, message = udp_document()
	document = open_document(image, message)

	with pytest.raises(Refused) as why:
		document.set("length", 40)
	assert "writing this moves udp_header.payload" in str(why.value)
	assert "0034" in str(why.value)

	# Refused means refused: not a byte of it landed.
	assert document.buffer.hex() == "123400350008abcd"

	# And the member beside it, which shifts nothing, still writes.
	document.set("destination_port", 4242)
	assert document.buffer.hex() == "123410920008abcd"


def test_a_write_needs_a_mutable_buffer_and_says_so() -> None:
	"""A view may be over `bytes`, which the read path is happy with.

	mypy names it -- "unsupported target for indexed assignment" -- and
	without the check the caller gets a `TypeError` out of the walker's
	insides instead of a refusal saying what it needs. Which is what
	happened while this was being written.
	"""
	from walker.image import load
	from walker.walk import acquire, write_scalar

	image, message = udp_document()
	loaded = load(image)
	view   = acquire(loaded, bytes(message), 0)	# immutable on purpose

	with pytest.raises(Refused) as why:
		write_scalar(view, 1, 4242)
	assert "immutable bytes" in str(why.value)


# -- byte runs (26.180) -----------------------------------------------------


def udp_with_payload() -> tuple[bytes, bytearray]:
	schema  = (ROOT / "example/udp/udp.situ").read_text(encoding="ascii")
	parsed  = parse_text(schema, path="udp.situ")
	image, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)
	return image, bytearray(bytes.fromhex("12340035000cabcddeadbeef"))


def test_a_run_written_at_its_own_length_moves_nothing() -> None:
	"""A payload, a name, a magic: a run is what a file editor most often
	edits, and one written at the length it already has is the same class of
	safety as a fixed scalar in place."""
	image, message = udp_with_payload()
	document = open_document(image, message)

	notes = document.set("payload", bytes.fromhex("cafebabe"))

	assert document.buffer.hex() == "12340035000cabcdcafebabe"
	assert notes and "stale" in notes[0]


def test_a_run_written_at_another_length_is_refused() -> None:
	"""The length is the whole guard: shorter or longer is a layout change
	however it is spelled, so it is refused by count."""
	image, message = udp_with_payload()
	document = open_document(image, message)

	for value in (bytes.fromhex("cafe"), bytes.fromhex("cafebabecafe")):
		with pytest.raises(Refused) as why:
			document.set("payload", value)
		assert "changing a run's length moves what follows it" in str(why.value)

	assert document.buffer.hex() == "12340035000cabcddeadbeef"


def test_the_marker_is_the_field_and_the_refusal_is_the_write() -> None:
	"""`payload` shows `[moves]` and a same-length write to it succeeds.

	`mutate = Shifting` says a write to this member *may* move what follows,
	which is the field's character. Whether a particular write does is a
	question about that write, and the extent comparison answers it. The two
	are different questions and the editor asks both.
	"""
	image, message = udp_with_payload()
	document = open_document(image, message)
	payload  = next(f for f in document.fields() if f.name == "payload")

	assert payload.mutate == "Shifting"
	assert "the bytes after it move" in payload.write_cost
	document.set("payload", bytes.fromhex("cafebabe"))


def test_set_bytes_reaches_the_cli(tmp_path: Path) -> None:
	(tmp_path / "m.bin").write_bytes(bytes.fromhex("12340035000cabcddeadbeef"))

	done = run("situ-edit", str(ROOT / "example/udp/udp.situ"),
	           str(tmp_path / "m.bin"), "--set-bytes", "payload=cafebabe",
	           "--out", str(tmp_path / "out.bin"))

	assert done.returncode == 0, done.stderr
	assert (tmp_path / "out.bin").read_bytes().hex() == \
		"12340035000cabcdcafebabe"

	bad = run("situ-edit", str(ROOT / "example/udp/udp.situ"),
	          str(tmp_path / "m.bin"), "--set-bytes", "payload=zz")
	assert bad.returncode == 1
	assert "is not hex" in bad.stderr
