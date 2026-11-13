"""Every element of an enum array is one of its members (26.1013).

An enum that rejects unknown values was held to that for one value and for
no array of them, in any of the six readers -- and C did worse for a run
the message counts: it checked the run as one scalar, `_is_known(<run>_get(
view))`, through a getter no array has, and did not compile. Rust's indexed
getter for a wide enum array was declared `Result<u16>` and returned
`Option<E>`, so it did not compile either. `situc verify` named enum arrays
as unread, which was true.

Each case puts an unknown value in one place: a counted byte-wide run, a
fixed one, a counted 16-bit one (big-endian, so a byte order mistake would
also show), and a single member for the control.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway
from test_walker_c import (COMPILER, _inline_image, c_verdict, python_verdict,
                           shape_named_text)

SCHEMA = """target buffer;
endian big;
enum kind : u8 { a = 1, b = 2, }
enum wide : u16 { x = 1, y = 0x0102, }
struct m {
	u8   n;
	kind kinds[n];
	kind fixed[2];
	wide ws[n];
	kind single;
}
"""

GOOD = [2, 1, 2, 1, 2, 0x01, 0x02, 0x00, 0x01, 1]


def cases() -> list[tuple[str, bytes, str]]:
	found = [("ok", bytes(GOOD), "0")]
	for at, label in [(2, "counted"), (3, "fixed"), (6, "wide"), (9, "single")]:
		bad = list(GOOD)
		bad[at] = 9
		found.append((label, bytes(bad), "2"))
	# The wide run's members in the other byte order: 0x0201, not a member.
	swapped = list(GOOD)
	swapped[5], swapped[6] = 0x02, 0x01
	found.append(("wide swapped", bytes(swapped), "2"))
	return found


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_every_element(tmp_path: Path) -> None:
	source = tmp_path / "m.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for label, data, verdict in cases():
		said = {name: fourway.answers(argv, data, tmp_path)
		        for name, argv in command.items()}
		lines = {name: next(line for line in
		                    one[one.index("-- m\n"):].split("\n-- ")[0]
		                    .splitlines() if line.startswith("validate "))
		         for name, one in said.items()}
		assert set(lines.values()) == {f"validate {verdict}"}, (label, lines)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_both_walkers_read_every_element(tmp_path: Path) -> None:
	image = _inline_image(SCHEMA)
	shape = shape_named_text(SCHEMA, "m")
	for label, data, verdict in cases():
		assert python_verdict(image, data, shape) == verdict, label
		assert c_verdict(tmp_path, image, data, shape) == verdict, label
