"""An invariant maintaining a field after a member the data sizes (26.982).

`invariant s.z == size(s.v)` with `z` after `u8 v[n]` raised `offset is
dynamic` out of all four generators: the recompute stores at a static offset
and `z` has none. 16.1's answer for what a build cannot do -- no recompute,
the field still unwritable, the implied refusal still in `validate` -- is
what each emits now, and the refusal is the part asked here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import fourway

SCHEMA = """target buffer;
endian big;
struct s {
	u8 n;
	u8 v[n];
	u8 z;
}
invariant s.z == size(s.v);
"""


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_build_and_refuse_a_contradiction(tmp_path: Path) -> None:
	source = tmp_path / "unit.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	command = fourway.build(tmp_path, source)
	assert len(command) == 4
	for data, verdict in ((bytes([2, 9, 9, 2]), "0"),
	                      (bytes([2, 9, 9, 3]), "2")):
		for backend, argv in command.items():
			answer  = fourway.answers(argv, data, tmp_path)
			section = answer[answer.index("-- s\n"):].split("\n-- ")[0]
			line = next(one for one in section.splitlines()
			            if one.startswith("validate "))
			assert line.split()[1] == verdict, (data, backend, answer)
