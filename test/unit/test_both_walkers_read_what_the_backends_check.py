"""Three checks the backends make and the walkers did not (26.1137-26.1140).

Found replaying the fuzz corpora through the Python walker against C's
differ, each a member the four backends agree on and the walk read
differently; the C walker shared each fault but the last's cause.

- 26.1137: `[trim]` on a delimited text number. JSON's `number` is
  `scaled i64 value before ... [trim]`, and both walkers parsed the digits
  untrimmed, so ` 1`, `\\r1` and `1 ` were CONSTRAINT where all four read 1.
- 26.1138: a packed-decimal nibble above nine. DS1307's `year` has no
  bound, and `0xff` validated clean in both walkers while all four refused
  it; the getter decodes whatever the nibbles hold, so only a check sees it.
- 26.1140: the bytes a tag covers. `first..last` are placement indices,
  and a region's members are numbered after the struct's own, so the walk
  asking each member in the span to fit asked packet's own tag -- missing
  from a frame ending where the sealed body does -- and called a span
  wholly in the frame refused. The first fix skipped any member starting
  at the span's end, which skipped TCP's options -- the member that check
  exists for -- so the own members after the span's last own one are
  what is skipped, and TCP's case is held beside packet's.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

import fourway							# noqa: E402
from every_schema import load_schema				# noqa: E402
from situc import pack as packer				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from test_walker_c import (COMPILER, c_verdict, image_for,	# noqa: E402
                           python_verdict, shape_named)
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402

JSON = ROOT / "example" / "json" / "json.situ"
RTC  = ROOT / "example" / "rtc" / "rtc.situ"

#: A number with whitespace either side, which `[trim]` removes; and one
#: that is whitespace alone, which is no number.
TRIMMED = {"31": "0", "0d31": "0", "2031": "0", "3120": "0", "09310a": "0",
           "0d0d": "2"}

#: A DS1307 reading, and the same with a nibble above nine in `year`,
#: `day` and `weekday`'s neighbour `hours`.
CLOCK = {"00000006060699": "0", "000000062602ff": "2",
         "00000006070a2e": "2", "0000000606069f": "2"}


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
@pytest.mark.parametrize("schema, struct, cases",
                         [(JSON, "number", TRIMMED),
                          (RTC, "wall_clock", CLOCK)],
                         ids=["trim", "bcd"])
def test_both_walkers_judge_it_as_the_backends_do(schema: Path, struct: str,
		cases: dict[str, str], tmp_path: Path) -> None:
	image = image_for(schema)
	shape = shape_named(schema, struct)
	for message, verdict in cases.items():
		data = bytes.fromhex(message)
		assert python_verdict(image, data, shape) == verdict, message
		assert c_verdict(tmp_path, image, data, shape) == verdict, message


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs all four toolchains")
@pytest.mark.parametrize("schema, struct, cases",
                         [(JSON, "number", TRIMMED),
                          (RTC, "wall_clock", CLOCK)],
                         ids=["trim", "bcd"])
def test_the_backends_say_so_too(schema: Path, struct: str,
		cases: dict[str, str], tmp_path: Path) -> None:
	command = fourway.build(tmp_path, schema)
	for message, verdict in cases.items():
		for name, argv in command.items():
			said = fourway.answers(argv, bytes.fromhex(message), tmp_path)
			lines = said.splitlines()
			at = lines.index(f"-- {struct}")
			got = next(line for line in lines[at + 1:]
			           if line.startswith("validate"))
			assert got == f"validate {verdict}", (name, message)


#: packet's header, nonce and sealed body to exactly the frame's end, with
#: no room for the tag after them: the bytes it covers are all there.
PACKET = bytes.fromhex("7e000000" "01010010" "00000000" + "00" * 12
                       + "00" * 22 + "00" * 16)


def test_a_span_in_the_frame_is_not_refused_for_the_tag_after_it() -> None:
	schema = ROOT / "example" / "packet" / "packet.situ"
	parsed = load_schema(schema)
	image = load(packer.pack(parsed, resolve(parsed, solve(parsed)),
	                         metadata=True)[0])
	listing = report.listing(image, PACKET)
	lines = listing.splitlines()
	at = lines.index("-- packet")
	covered = [line for line in lines[at:] if ".covered" in line][0]
	assert covered == f"tag.covered 4+{len(PACKET) - 4}", covered


#: A TCP header whose data offset claims 32 bytes of options past a 20-byte
#: frame: the covered span ends in the frame, its options do not (26.1058).
TCP = bytes.fromhex("0200000000000000ffdddddddddddddddddd02cc")


def test_a_member_running_past_the_frame_still_refuses_the_span() -> None:
	schema = ROOT / "example" / "tcp" / "tcp.situ"
	parsed = load_schema(schema)
	image = load(packer.pack(parsed, resolve(parsed, solve(parsed)),
	                         metadata=True)[0])
	lines = report.listing(image, TCP).splitlines()
	assert "checksum.covered refused" in lines, lines
