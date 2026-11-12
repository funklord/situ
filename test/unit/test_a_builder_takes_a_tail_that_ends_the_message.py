"""A builder takes an arm that ends in `[remaining]` where it ends the message
(26.973).

fuzznet's call frames, one message a datagram:

    struct frame { u8 stream; u16 pieces [min = 1]; u8 media[remaining]; }
    struct message { u8 family [must_eq = 2]; kind kind;
        variant body switch (kind) { case kind.ring: nothing ring;
                                     case kind.frame: frame frame;
                                     default: error; } }

`--layer edit` refused the `frame` builder: an arm's value is bytes the
caller built, checked by the arm struct's `_required`, and a struct that
ends where its view does has none. Where the variant ends the message,
the bytes' end is the message's end, so they are one whole `frame`
however long, if they validate as one. Anywhere else the refusal stands.

Expected bytes are laid out by hand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Under another name: imported as `test_...` pytest collects it here too,
# with its corpus-wide parametrisation.
from test_the_four_builders_agree import (
	test_four_builders_write_one_message as four_builders_agree)

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
enum kind : u8 { ring = 1, frame = 5, default = error, }
struct frame { u8 stream; u16 pieces [min = 1]; u8 media[remaining]; }
struct nothing { }
struct message {
	u8 family [must_eq = 2];
	kind kind;
	variant body switch (kind) {
		case kind.ring:  nothing ring;
		case kind.frame: frame   frame;
		default: error;
	}
}
"""


def _python_builder(tmp_path: Path, schema_text: str) -> object:
	schema = tmp_path / "message.situ"
	schema.write_text(schema_text, encoding="ascii")
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "python", "--layer", "edit", str(schema), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True)
	assert done.returncode == 0, done.stderr
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		import message_build  # type: ignore[import-not-found]
		return message_build
	finally:
		sys.path.remove(str(tmp_path))
		sys.path.remove(str(ROOT / "runtime" / "python"))
		sys.modules.pop("message_build", None)
		sys.modules.pop("message", None)


def test_the_frame_is_written_as_the_caller_built_it(tmp_path: Path) -> None:
	built = _python_builder(tmp_path, SCHEMA)
	frame = bytearray(16)
	n = built.build_frame(frame, 7, 1, b"\xaa\xbb\xcc")    # type: ignore[attr-defined]
	assert bytes(frame[:n]) == bytes.fromhex("07 0001 aabbcc".replace(" ", ""))

	out = bytearray(32)
	n = built.build_message_frame(out, 2, bytes(frame[:n]))  # type: ignore[attr-defined]
	assert bytes(out[:n]) == bytes.fromhex("02 05 07 0001 aabbcc".replace(" ", ""))

	# No media at all is a frame too: the datagram ends after `pieces`.
	n = built.build_message_frame(out, 2, bytes.fromhex("070001"))  # type: ignore[attr-defined]
	assert bytes(out[:n]) == bytes.fromhex("0205070001")


def test_bytes_that_are_no_frame_are_refused(tmp_path: Path) -> None:
	built = _python_builder(tmp_path, SCHEMA)
	out = bytearray(32)
	for blob in (bytes.fromhex("070000aa"),     # `pieces` under its min
	             bytes.fromhex("0700")):        # shorter than a frame
		with pytest.raises(built.ConstraintError):  # type: ignore[attr-defined]
			built.build_message_frame(out, 2, blob)  # type: ignore[attr-defined]


def test_an_arm_that_does_not_end_the_message_is_still_refused(
		tmp_path: Path) -> None:
	"""A byte after the variant: a blob one byte too long would swallow it,
	and nothing in a `[remaining]` tail says where it stops."""
	trailing = SCHEMA.replace("\t\tdefault: error;\n\t}\n",
	                          "\t\tdefault: error;\n\t}\n\tu8 crc;\n")
	schema = tmp_path / "message.situ"
	schema.write_text(trailing, encoding="ascii")
	done = subprocess.run([sys.executable, "-m", "situc", "build", "--target",
	                       "python", "--layer", "edit", str(schema), "--out",
	                       str(tmp_path)], cwd=ROOT, capture_output=True,
	                      text=True)
	assert "no builder for `message.frame`" in done.stderr, done.stderr


def test_the_four_builders_agree_about_it(tmp_path: Path) -> None:
	schema = tmp_path / "message.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	(tmp_path / "four").mkdir()
	four_builders_agree(schema, tmp_path / "four")
