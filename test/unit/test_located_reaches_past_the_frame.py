"""`at off` is not a claim about this frame (9.8, 26.446).

`project.md` has said since 26.424 that "a `located` member reaches past
the frame by construction", and five of the six readers had each
independently assumed otherwise -- in three separate places, failing in
two opposite directions.

What this holds is the *narrowness* of the exclusion as much as the
exclusion. The checks being skipped are real checks that a non-located
member at a dynamic offset must still fail, so every case here has a
control that differs from it in nothing but `at`, and the control has to
go on being refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from situc import pack as packer
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT
from fourway import COMPLETE, answers, build

sys.path.insert(0, str(ROOT))
from walker import report                                   # noqa: E402
from walker.image import Image, load                        # noqa: E402
from walker.walk import Refused, acquire                    # noqa: E402


PREAMBLE = "target buffer;\nendian big;\n"

#: A fixed-size run at an offset the message chooses, which is the pair
#: every reader keys on -- `at off` satisfies both halves by construction.
LOCATED = PREAMBLE + """
struct s {
	u8 off;
	u8 payload[2] at off;
	u8 tail;
}
"""

#: The control, and the only difference is `at`. `tag` is four fixed bytes
#: at an offset `n` decides, so it is the same pair without the construct
#: that excuses it.
PLAIN = PREAMBLE + """
struct s {
	u8 n;
	u8 v[n];
	u8 tag[4];
}
"""


def image_of(text: str) -> Image:
	parsed = parse_text(text)
	blob, _ = packer.pack(parsed, resolve(parsed, solve(parsed)),
	                      metadata=True)
	return load(blob)


def verdict(text: str, message: bytes) -> object:
	"""What the Python walk says about `message`, or `no-view`."""
	image = image_of(text)
	try:
		view = acquire(image, message, 0)
	except Refused:
		return "no-view"
	return report._validate(image, view, 0)


def carrying(text: str, check: int) -> set[str]:
	"""Which members carry `check` in the packed image, by name.

	Names rather than a count, because a count is the same shape as a gate
	over an empty file list: the first version of the control below
	asserted one row and there are two -- `v[n]` declares a length like
	any other run -- and a number cannot say which one went missing.
	"""
	image = image_of(text)
	return {report._local(image, index)
	        for index in range(len(image.placements))
	        for carried, _ in image.constraints.get(index, ())
	        if carried == check}


def test_the_packer_writes_no_fits_frame_row_for_a_located_member() -> None:
	"""The third copy of the assumption, and the quietest.

	It never fired while the walkers' own arithmetic fired first, which is
	the reason to mind it rather than to leave it: with the walkers fixed
	and this row still written, the Python walk goes back to refusing.
	A check hidden behind another check is not a check that has been
	removed.
	"""
	assert carrying(LOCATED, report.FITS_FRAME) == set()


def test_the_packer_still_writes_one_for_a_run_that_is_not_located() -> None:
	assert carrying(PLAIN, report.FITS_FRAME) == {"v", "tag"}


def test_an_out_of_range_located_offset_is_not_a_malformed_message() -> None:
	"""`off = 255` in a three-byte frame, which all four backends accept.

	The accessor asks the message on every call and answers len=0, which
	is where 9.8 puts the question. Both walkers answered BOUNDS from
	their own fixed-size-at-a-dynamic-offset arithmetic, so the fifth and
	sixth descriptions refused a frame the other four read.
	"""
	assert verdict(LOCATED, bytes.fromhex("ff0203")) == report.OK


def test_a_located_member_inside_the_frame_is_accepted_too() -> None:
	"""The other half of the pair: the fix must not have made it blind."""
	assert verdict(LOCATED, bytes.fromhex("0102030405")) == report.OK


def test_a_fixed_run_at_a_dynamic_offset_past_the_end_is_still_bounds() -> None:
	"""The control, and it has to clear the struct's own minimum.

	The first control written for this did not: four bytes is short of the
	five `s` needs, so the view was refused on entry and the check under
	test never ran. It passed, and it would have passed with the check
	deleted. Six bytes with `n = 4` leaves this the only thing that can
	answer.
	"""
	assert verdict(PLAIN, bytes.fromhex("04aabbccdd01")) == report.ERR_BOUNDS


def test_the_same_control_is_clean_when_the_run_fits() -> None:
	assert verdict(PLAIN, bytes.fromhex("01aa01020304")) == report.OK


def test_a_truncated_bmp_is_read_rather_than_refused() -> None:
	"""The corpus payoff, in the worked example the construct exists for.

	BMP places its pixels `at file.pixel_offset`, and its image is the one
	corpus image this changed -- by exactly the sixteen bytes of one row.
	A file truncated below its declared pixel data was refused by the walk
	and accepted by all four backends.

	This is a permissive change and it is the design's: a schema wanting
	the stricter answer says so with `require` against the file's own
	declared size, which is what BMP's two assertions already do.
	"""
	text  = (ROOT / "example" / "bmp" / "bmp.situ").read_text()
	whole = bytes.fromhex(
		"424D4E000000000000003600000028000000030000000200000001001800"
		"000000001800000000000000000000000000000000000000"
		"0000FF0000FF0000FF0000000000FF0000FF0000FF000000")
	assert len(whole) == 78
	image = image_of(text)
	for message in (whole, whole[:60]):
		view = acquire(image, message, 2)
		assert report._validate(image, view, 2) == report.OK


def _validate_line(text: str, struct: str) -> str:
	"""The `validate` a driver printed under `-- struct`."""
	seen = False
	for line in text.splitlines():
		if line.startswith("-- "):
			seen = line == f"-- {struct}"
		elif seen and line.startswith("validate"):
			return line.split()[-1]
		elif seen and line in ("no-view", "needs-arguments"):
			return line
	return "absent"


@pytest.mark.skipif(not COMPLETE, reason="needs all four toolchains")
def test_all_four_backends_read_a_located_member_alike(tmp_path: Path) -> None:
	"""The backend half of 26.446, which failed the other way round.

	C++ and Python emitted a fits-the-frame check for a located run with a
	literal count and refused a frame the other two accept -- `validate` 1
	against 0. The packed image and both walkers are the other three
	readers and are covered above; this is the two that compile.

	`located_tail` in `edges.situ` is the same construct in the corpus, so
	the random-bytes differential carries it on every commit too. This
	asks with chosen bytes rather than drawn ones, because the case that
	separated the backends -- an offset the frame does not reach -- is one
	a draw finds only by luck.
	"""
	schema = tmp_path / "s.situ"
	schema.write_text(LOCATED, encoding="ascii")
	command = build(tmp_path, schema)

	for message, want in ((bytes.fromhex("0102030405"), "0"),
	                      (bytes.fromhex("ff0203"),     "0")):
		said = {name: _validate_line(answers(cmd, message, tmp_path), "s")
		        for name, cmd in command.items()}
		assert set(said.values()) == {want}, (message.hex(), said)
