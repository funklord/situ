"""Nothing is asked after a region whose run is sized from inside it
(26.758).

`u8 n; u16 vals[n]`, both inside a sealed region, puts `n` behind the
gate: the arithmetic that places the tag after the region runs on the plain
view, where `n` is ciphertext. All four backends decline the tag and the
member after it (26.440), and the differ's `TAG` probe asked the schema
rather than the emitters and named both -- so the C driver did not compile,
and the schema was compared by nobody. 26.752 recorded it open.

The differ now stops after such a region, as it already did for a walked
interior and a self-delimiting one. Each form below builds all four drivers
and they agree; with the fix reverted, C does not build.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

HEAD = """target buffer;
endian big;
codec sealing_aead {
	length_preserving;
	seekable;
	authenticated;
	invertible;
	deterministic;
}
impl sealing_aead extern "my_sealing_aead";
codec doubling {
	expansion = ratio_exact(2, 1);
	granularity = byte;
	seekable = linear;
	invertible;
	deterministic;
}
impl doubling extern "my_doubling";
"""

FORMS = {
	"sealed_count": """struct inside {
	u8  kind;
	sealed body(sealing_aead) {
		u8   n;
		u16  vals[n];
	}
	tag u8  mac[16] covers(body);
	u8  after;
}
""",
	"sealed_arithmetic": """struct inside {
	u8  kind;
	sealed body(sealing_aead) {
		u8   n;
		u16  vals[n + 1];
	}
	tag u8  mac[16] covers(body);
	u8  after;
}
""",
	"coded_count": """struct inside {
	u8  kind;
	coded body(doubling) {
		u8  n;
		u8  vals[n];
	}
	u8  after;
}
""",
}

PACKET = bytes([1, 2, 0, 1, 0, 2]) + bytes(16) + b"\x07"


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_four_drivers_build_and_agree(form: str, tmp_path: Path) -> None:
	schema = tmp_path / "unit.situ"
	schema.write_text(HEAD + FORMS[form], encoding="ascii")
	command = fourway.build(tmp_path, schema)
	assert set(command) == {"c", "cpp", "rust", "python"}, command
	given = {name: fourway.answers(argv, PACKET, tmp_path)
	         for name, argv in command.items()}
	first = next(iter(given.values()))
	assert all(answer == first for answer in given.values()), given
	# What follows the region is asked of nobody: no backend places it.
	assert "\nmac" not in first and "\nafter" not in first, first
	assert "\nkind 1\n" in first, first
