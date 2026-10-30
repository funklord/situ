"""The differ's gated byte probe asks only for readers all four emit.

The `SEALED` probe asks each byte run inside a gate for its length and last
byte. It asked every run with a count, and the backends split on three
kinds of run: a FIXED one (C and Rust read it, C++ and Python do not), one
sized by a field inside the region (Rust declines it) and a `[remaining]`
one (C++ declines it). A driver asking for any of them did not compile --
found by the full composed sweep in every sealed cell of those shapes
(26.750).

So the probe asks only for runs sized by fields OUTSIDE the region, which
all four read; the split itself is open in `project.md`. This builds all
four drivers over a gate holding a fixed run and one sized from outside,
holds them to agreement, and checks the corpus's three gated probes are
still asked. A run sized from INSIDE the region is left out: C cannot
place the tag after it either, declines that accessor too, and the driver
asking for it is the same fault one member over -- recorded in 26.750.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import fourway						# noqa: E402
from situc.codegen import differ			# noqa: E402
from situc.diagnostics import SituError			# noqa: E402

from every_schema import SCHEMAS			# noqa: E402
from test_four_backends_frame_alike import analysed	# noqa: E402

SCHEMA = """target buffer;
endian big;
codec seal { length_preserving; seekable; authenticated; invertible;
             deterministic; }
impl seal extern "my_seal";
struct g {
	u8  n;
	sealed body(seal) {
		u8  fixed[2];
		u8  outside[n];
	}
	tag u8 mac[16] covers(body);
}
"""


def test_the_drivers_build_and_agree_over_a_fixed_run(tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.fail(f"a driver asked for a reader its backend lacks: {failed}")
	packet = bytes([2, 0xAA, 0xBB, 0x01, 0x02]) + bytes(16)
	given = {name: fourway.answers(argv, packet, tmp_path)
	         for name, argv in command.items()}
	first = next(iter(given.values()))
	assert all(answer == first for answer in given.values()), given
	assert "outside len=2 last=2" in first.splitlines(), first
	# Asked again since 26.752: Python reads a gated fixed run now, and C
	# answers its length as the constant it is.
	assert "fixed len=2 last=187" in first.splitlines(), first


def test_the_corpus_gated_probes_are_still_asked() -> None:
	"""`dtls.record`'s `fragment[length - 24]` is sized by an expression,
	and a first version testing `sized_by` alone dropped it."""
	asked: set[str] = set()
	for path in SCHEMAS:
		_parsed, resolved = analysed(path)
		try:
			structs = differ.structs_of(resolved)
		except SituError:
			continue
		for struct in structs:
			for ask in differ.asks(struct, set(resolved.structs),
			                       resolved.structs):
				asked.update(f"{struct.name}.{one}"
				             for one, _count in ask.inside_bytes)
	assert {"record.fragment", "keystore.plaintext",
	        "packet.body"} <= asked, asked


REMAINING = """target buffer;
endian big;
codec seal { length_preserving; seekable; authenticated; invertible;
             deterministic; }
impl seal extern "my_seal";
struct s {
	u8  lead;
	sealed body(seal) {
		u8  run[remaining];
	}
	tag u8 mac[16] covers(body);
}
"""


def test_a_remaining_run_in_a_gate_is_read_by_all_four(tmp_path: Path) -> None:
	"""C++ declined it -- "this backend cannot resolve its extent" -- while
	the other three read it, and the C++ driver asking for it did not
	compile (26.752)."""
	schema = tmp_path / "unit.situ"
	schema.write_text(REMAINING, encoding="ascii")
	try:
		command = fourway.build(tmp_path, schema)
	except fourway.BuildFailed as failed:
		pytest.fail(f"a driver asked for a reader its backend lacks: {failed}")
	for packet in (bytes([1, 0x10, 0x20, 0x30]) + bytes(16),
	               bytes([1]) + bytes(16)):
		given = {name: fourway.answers(argv, packet, tmp_path)
		         for name, argv in command.items()}
		first = next(iter(given.values()))
		assert all(answer == first for answer in given.values()), given
