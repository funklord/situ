"""A struct with a `parameter` member, in the reference frontend (26.598).

`report.SUPPORTED` names `needs-arguments` as one of its seventeen probes,
and it is the one the sweep in 26.597 structurally could not see: it is not
a row but a verdict on the whole struct, printed in place of every row.

This frontend had no channel for an argument at all. `acquire` refuses a
short argument list at the door, nothing above it checked, and the view is
lazy -- so the document OPENED and then raised `Unsupplied` out of `render`,
and the tool printed a Python traceback. A reader holding a capture of a
negotiated stream got a stack trace where the answer is *you have not said
what the block size was*.

	situ-edit: struct `negotiated` takes 1 argument(s) and 0 were given:
	           `block`, in that order
	situ-edit: pass --arg once per argument, in that order

The walker keeps `no-view` and `needs-arguments` apart deliberately, and
the last test here pins the hierarchy that makes that possible: `Unsupplied`
is not a `Refused`, so a handler for one cannot silently absorb the other.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from editor.document import open_document		# noqa: E402
from editor.text import render				# noqa: E402
from walker.walk import Refused, Unsupplied		# noqa: E402

#: One `[stream]` parameter and a run whose length is a function of it,
#: which is 0050's own case: the layout after the parameter cannot be known
#: without it, so a value nobody supplied is a layout nobody chose.
SCHEMA = """target buffer;
endian big;

struct negotiated {
	parameter u16 block [stream];
	u8        payload[block];
	u16       checksum;
}
"""

MESSAGE = bytes(range(1, 17))

#: Too short for `payload[4]`, which makes `owned.decode` refuse the WHOLE
#: message -- it is whole-or-nothing by design. That is the only state in
#: which the parameter row's value can come from anywhere but `decode`, and
#: the first version of the test below used the long message instead: it
#: passed, and went on passing with the branch it was written for disabled,
#: because `decode` answers `block` itself when it answers at all.
TOO_SHORT = bytes(range(1, 4))


def image() -> bytes:
	parsed   = parser.parse(Source("t.situ", SCHEMA))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return blob


def test_opening_without_the_argument_is_refused() -> None:
	"""At the door, not at the first read. Every answer below would be
	about a layout the caller never chose."""
	with pytest.raises(Unsupplied) as caught:
		open_document(image(), MESSAGE, "negotiated")
	assert "1 argument" in str(caught.value)


def test_the_refusal_names_the_parameter() -> None:
	"""`acquire` can only count them -- a device omits the image's tail, so
	an argument keyed by name would be one only a tooling walker could
	take. This IS the tooling walker, and *takes 1 argument(s)* is not
	something a person holding a capture can act on."""
	with pytest.raises(Unsupplied) as caught:
		open_document(image(), MESSAGE, "negotiated")
	assert "`block`" in str(caught.value)


def test_too_many_is_refused_as_well() -> None:
	"""Read backwards, it is the same fault: an argument nothing declares
	is one the caller believes is being used."""
	with pytest.raises(Unsupplied):
		open_document(image(), MESSAGE, "negotiated", args=(4, 5))


def test_the_argument_reaches_the_view() -> None:
	"""The point of the channel: with it supplied, the run it sizes reads."""
	document = open_document(image(), MESSAGE, "negotiated", args=(4,))
	rows = {field.name: field for field in document.fields()}
	assert rows["payload"].size == 4
	assert rows["payload"].value == MESSAGE[:4]


def test_a_parameter_row_carries_what_the_caller_gave() -> None:
	"""Against a message `decode` refuses, which is the state that reaches
	the hazard.

	`_values` is whole-or-nothing: one unreadable member and every row
	loses its value. A parameter is the one member whose value does not
	come from the message at all, so the row said *cannot be read* about
	the number the caller had just typed -- while the two rows either side
	of it said it truthfully.
	"""
	document = open_document(image(), TOO_SHORT, "negotiated", args=(4,))
	rows = {field.name: field for field in document.fields()}
	assert rows["payload"].value is None, "the fixture must refuse `decode`"
	assert rows["block"].value == 4
	assert "cannot be read" not in (rows["block"].note or "")


def test_the_parameters_are_named_in_the_order_to_pass_them() -> None:
	document = open_document(image(), MESSAGE, "negotiated", args=(4,))
	assert document.parameters() == ["block"]


def test_render_does_not_raise_once_the_argument_is_given() -> None:
	"""Where the traceback came from: the view is lazy, so the document
	opened and `render` was the first thing to ask for it."""
	document = open_document(image(), MESSAGE, "negotiated", args=(4,))
	assert any("payload" in line for line in render(document))


def test_the_cli_says_so_rather_than_crashing(tmp_path: Path) -> None:
	"""The whole finding, from outside: exit 1 and a sentence, where it
	printed a stack trace."""
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)
	done = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"),
		 str(tmp_path / "s.situ"), str(tmp_path / "m.bin")],
		capture_output=True, text=True, cwd=ROOT, timeout=300)

	assert done.returncode == 1
	assert "Traceback" not in done.stderr
	assert "`block`" in done.stderr
	assert "--arg" in done.stderr


def test_the_cli_takes_the_argument(tmp_path: Path) -> None:
	(tmp_path / "s.situ").write_text(SCHEMA, encoding="ascii")
	(tmp_path / "m.bin").write_bytes(MESSAGE)
	done = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"),
		 str(tmp_path / "s.situ"), str(tmp_path / "m.bin"), "--arg", "4"],
		capture_output=True, text=True, cwd=ROOT, timeout=300)

	assert done.returncode == 0, done.stderr
	assert "payload" in done.stdout
	assert "block" in done.stdout


def test_the_two_refusals_are_not_one() -> None:
	"""`no-view` is a frame too short for the struct; `needs-arguments` is
	a caller who has not said which layout the bytes are in. Reporting the
	second as the first is a verdict on bytes nobody read, which is why
	`report.listing` gives it its own line -- and why a handler for
	`Refused` must not catch this.
	"""
	assert not issubclass(Unsupplied, Refused)
	assert not issubclass(Refused, Unsupplied)
