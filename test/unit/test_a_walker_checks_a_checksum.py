"""Both walkers compute a checksum from the image, and agree (26.1058).

The Python walker could compute a derived checksum since 26.579 and only
`situ-edit` asked it to; the C walker read none of the three tables that
say how. So the two independent readers of a layout never checked the one
member whose value is a function of the others, and the four backends'
`_compute` was compared with nothing.

Three things are pinned here, each by a witness that is not situ's:

	PNG    the IEND chunk's CRC, `AE 42 60 82` in every PNG ever written
	IPv4   RFC 791's worked header, checksum `B8 61`
	ICMP   an echo request of zeros, whose RFC 1071 sum is `F7 FF`

and then every corpus schema with a computable tag, both walkers member
for member over random and planted messages -- with the count of computed
sums asserted, because two walkers that both decline everything agree.

Since 26.1066 both are handed a pseudo-header, so TCP's and UDP's sums are
computed rather than declined: held here to RFC 1071 written out below
rather than to any of situ's three implementations of it.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from every_schema import SCHEMAS, ids				# noqa: E402
from fourway import draw, planted				# noqa: E402
from situc.codegen import differ				# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from test_walker_c import (COMPILER, _build_driver, _run_driver,	# noqa: E402
                           image_for, shape_named)
from walker import report					# noqa: E402
from walker.image import load					# noqa: E402
from walker.walk import Refused, Unplaceable, Unsupplied, acquire		# noqa: E402

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")

#: One line per member: `-` for a member that is not a tag, else what
#: `situ_walk_sum_prefixed` answered over `PREFIX`, which `sums_over`
#: fills in.
SUMS_OVER = """situ_walk_placement held;
		if (situ_walk_placement_at(&image, first + i, &held) != SITU_WALK_OK) {
			return 1;
		}
		if ((held.flags & SITU_WALK_IS_TAG) == 0u) {
			printf("-\\n");
			continue;
		}
		uint32_t sum = 0u;
		static const uint8_t ahead[] = { PREFIX };
		const situ_walk_err got = situ_walk_sum_prefixed(
			&image, msg, len, shape, first + i, ahead,
			(uint32_t)(sizeof ahead - 1u), &sum);
		if (got == SITU_WALK_OK) {
			printf("sum=%u\\n", sum);
		} else if (got == SITU_WALK_UNSUPPORTED) {
			printf("cannot\\n");
		} else {
			printf("refused\\n");
		}"""


def sums_over(prefix: bytes | None) -> str:
	"""`SUMS` handed `prefix`. The array carries one byte more than the
	prefix, so C has an initialiser where there is none and the length is
	what was meant; no prefix is the empty one, which a tag that names a
	prefix refuses as a wrong length rather than declining -- so the sweep
	hands one wherever a schema's tags name it."""
	held = ", ".join(f"0x{b:02x}" for b in (prefix or b"")) + ", 0"
	return SUMS_OVER.replace("{ PREFIX }", "{ " + held.lstrip(", ") + " }")


#: The driver with no prefix, which other files import.
SUMS = sums_over(None)


def rfc1071(data: bytes) -> int:
	"""RFC 1071 section 4.1, complemented, as TCP and UDP send it -- with
	UDP's rule that a computed zero goes out as all ones, which neither
	vector below reaches."""
	if len(data) % 2:
		data += b"\0"
	total = sum(data[i] << 8 | data[i + 1] for i in range(0, len(data), 2))
	while total >> 16:
		total = (total & 0xFFFF) + (total >> 16)
	return (~total & 0xFFFF) or 0xFFFF


def python_sums(blob: bytes, message: bytes, shape: int,
		prefix: bytes | None = None) -> list[str]:
	"""The same lines from `report._sum`, which the listing prints."""
	image = load(blob)
	view  = acquire(image, message, shape)
	found = []
	for index in image.members(image.structs[shape]):
		if not image.placements[index].is_tag:
			found.append("-")
			continue
		try:
			said = report._sum(image, view, index, "t", prefix)
		except (Refused, Unplaceable):
			said = ["t.sum refused"]
		if not said:
			found.append("cannot")
		elif said[0] == "t.sum refused":
			found.append("refused")
		else:
			found.append(f"sum={said[0].split()[-1]}")
	return found


#: UDP from 10.0.0.1 to 10.0.0.2, ports 0x1234 and 0x5678, four bytes of
#: payload; the pseudo-header is RFC 768's -- both addresses, a zero, the
#: protocol and the UDP length.
UDP_PSEUDO  = "0a000001" "0a000002" "00" "11" "000c"
UDP_SEGMENT = "1234" "5678" "000c" "0000" "deadbeef"

#: A bare SYN with no options and three bytes of text, an odd length so the
#: last byte is the high half of a word; RFC 793's pseudo-header carries the
#: segment length, 23.
TCP_PSEUDO  = "c0a80001" "c0a800c7" "00" "06" "0017"
TCP_SEGMENT = ("0400" "0050" "00000001" "00000000" "50" "02" "ffff" "0000"
               "0000" "616263")

KNOWN = (
	("example/png/png.situ", "chunk",
	 "0000000049454E44AE426082", 0xAE426082, ""),
	("example/ipv4/ipv4.situ", "ipv4_header",
	 "45000073000040004011b861c0a80001c0a800c7", 0xB861, ""),
	("example/icmp/icmp.situ", "icmp_message",
	 "0800f7ff00000000", 0xF7FF, ""),
	("example/udp/udp.situ", "udp_header", UDP_SEGMENT,
	 rfc1071(bytes.fromhex(UDP_PSEUDO + UDP_SEGMENT)), UDP_PSEUDO),
	("example/tcp/tcp.situ", "tcp_header", TCP_SEGMENT,
	 rfc1071(bytes.fromhex(TCP_PSEUDO + TCP_SEGMENT)), TCP_PSEUDO),
)


@pytest.mark.parametrize("schema,struct,packet,expected,prefix", KNOWN,
                         ids=[one[1] for one in KNOWN])
def test_both_walkers_compute_the_published_checksum(
		schema: str, struct: str, packet: str, expected: int, prefix: str,
		tmp_path: Path) -> None:
	path  = ROOT / schema
	blob  = image_for(path)
	shape = shape_named(path, struct)
	message = bytes.fromhex(packet)
	ahead   = bytes.fromhex(prefix) if prefix else None

	drive = _build_driver(tmp_path, blob, sums_over(ahead))
	c_said  = _run_driver(drive, tmp_path, message, shape, by_line=True)
	py_said = python_sums(blob, message, shape, ahead)
	assert f"sum={expected}" in c_said, c_said
	assert c_said == py_said


@pytest.mark.parametrize("schema,struct,packet,prefix", [
	(one[0], one[1], one[2], one[4]) for one in KNOWN if one[4]],
	ids=[one[1] for one in KNOWN if one[4]])
def test_a_pseudo_header_is_summed_only_at_its_size(schema: str,
		struct: str, packet: str, prefix: str, tmp_path: Path) -> None:
	"""One byte short refuses, as each backend's `_compute` refuses it
	against `PREFIX_BYTES`; none at all declines, as both walkers did
	before 26.1066."""
	path  = ROOT / schema
	blob  = image_for(path)
	shape = shape_named(path, struct)
	message = bytes.fromhex(packet)
	short   = bytes.fromhex(prefix)[:-1]

	drive = _build_driver(tmp_path, blob, sums_over(short))
	c_said = _run_driver(drive, tmp_path, message, shape, by_line=True)
	assert "refused" in c_said, c_said
	assert c_said == python_sums(blob, message, shape, short)
	assert "cannot" in python_sums(blob, message, shape)


#: A declared run inside the coverage, then a run to the end of the frame.
OVERRUN = """codec internet_checksum {
	kernel = ones_complement(width = 16, complement);
}
impl internet_checksum derived;
target buffer;
endian big;

struct segment {
	authenticated summed {
		u8 count;
		checksum u8 checksum[2] covers(summed) is internet_checksum
			[self_as = 0];
		u8 options[count];
		u8 text[remaining];
	}
}
"""


@pytest.mark.parametrize("message,refused", [
	("0a0000aabb", True),
	("020000aabbcc", False),
], ids=["options-past-the-frame", "options-inside-it"])
def test_a_covered_member_past_the_frame_refuses_the_sum(message: str,
		refused: bool, tmp_path: Path) -> None:
	"""Ten bytes of options declared in a five-byte frame: every backend's
	`_covered` refuses the span (26.1058), and both walkers summed the
	five bytes there, because `text[remaining]` is placed back inside the
	frame and only the last member's end was asked. Found by TCP's options
	once the walkers were handed its pseudo-header (26.1066)."""
	from situc.pack import pack
	source   = parse(Source("overrun.situ", OVERRUN))
	resolved = resolve(source, solve(source))
	blob     = pack(source, resolved)[0]
	held     = bytes.fromhex(message)

	drive  = _build_driver(tmp_path, blob, SUMS)
	c_said = _run_driver(drive, tmp_path, held, 0, by_line=True)
	assert c_said == python_sums(blob, held, 0)
	assert ("refused" in c_said) is refused, c_said
	assert any(one.startswith("sum=") for one in c_said) is not refused


def _computable(path: Path) -> bool:
	text = path.read_text(encoding="ascii")
	if "checksum" not in text or "covers" not in text:
		return False
	image = load(image_for(path))
	return bool(image.tags) and bool(image.kernels)


SUMMED = [path for path in SCHEMAS if _computable(path)]

#: Per schema, how many sums the sweep compared as values.
COMPUTED: dict[str, int] = {}


@pytest.mark.parametrize("schema", SUMMED, ids=ids(SUMMED))
def test_the_walkers_agree_about_every_sum(schema: Path,
		tmp_path: Path) -> None:
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	blob     = image_for(schema)
	image    = load(blob)
	shapes   = sorted({shape for shape, held in enumerate(image.structs)
	                   if any(index in image.tags
	                          for index in image.members(held))})
	assert shapes, f"{schema.name}: a tag the image holds in no struct"

	# The pseudo-header the differential hands every reader (26.1066).
	ahead = differ.schema_prefix(resolved)
	drive = _build_driver(tmp_path, blob, sums_over(ahead))
	rng   = random.Random(20261117)
	sums  = 0
	for shape in shapes:
		for _ in range(12):
			message = planted(rng, parsed, resolved, draw(rng)) \
				if rng.random() < 0.5 else draw(rng)
			try:
				py_said = python_sums(blob, message, shape, ahead)
			except (Refused, Unplaceable, Unsupplied):
				continue
			c_said = _run_driver(drive, tmp_path, message, shape,
			                     by_line=True)
			assert c_said == py_said, (
				f"{schema.name} struct {shape} over {message.hex()}:\n"
				f"  C:      {c_said}\n  Python: {py_said}")
			sums += sum(one.startswith("sum=") for one in c_said)
	COMPUTED[schema.name] = sums


def test_the_sweep_computed_something() -> None:
	"""Two walkers that both decline everything agree, so the sweep has to
	have compared values. Measured when written: 84 sums over flac, icmp,
	ipv4, usb and edges. PNG's random lengths never fit and TCP's and UDP's
	sums run over a pseudo-header, so those three compare refusals -- and
	the published values above are what holds PNG's CRC."""
	if len(COMPUTED) < len(SUMMED):
		pytest.skip("the sweep did not run in full in this session")
	reached = sorted(name for name, sums in COMPUTED.items() if sums)
	assert len(reached) >= 5, reached
