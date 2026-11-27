"""`max(A, S) - S` is never negative, and the range check knows it.

HTTP/2's padded payload and pcapng's options each had a length "what the
parent leaves less a field": `max(n, 1 + p) - 1 - p`. The range check reads
an expression by interval, term by term, and called it possibly negative,
so both were spelt `max(n - 1 - p, 0)` around it (26.1182, 26.1184). The
form is now proved non-negative where the subtracted terms are exactly one
of `max`'s arguments, in any order -- a proof rather than a widening, so
`max(n, p) - q` is still refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fourway  # noqa: E402
from test_a_marked_integer_says_its_own_width import _all, _built  # noqa: E402

from situc.diagnostics import SituError, Source  # noqa: E402
from situc.layout import solve  # noqa: E402
from situc.parser import parse  # noqa: E402

HEAD = "target buffer;\nendian big;\n"
PROVED = HEAD + """
struct a { u8 n; u8 p; u8 d[max(n, 1 + p) - 1 - p]; u8 last; }
struct b { u8 n; u8 p; u8 d[max(n, p + 1) - p - 1]; u8 last; }
"""


def _solves(text: str) -> None:
	schema = parse(Source("unit.situ", text))
	solve(schema)


def test_the_proved_forms_are_accepted() -> None:
	_solves(PROVED)


@pytest.mark.parametrize("body", [
	"u8 d[max(n, p) - q];",          # a different term
	"u8 d[max(n, p) - p - 1];",      # one term more than the argument
	"u8 d[min(n, p + 1) - p - 1];",  # `min` proves nothing
])
def test_anything_else_is_still_refused(body: str) -> None:
	with pytest.raises(SituError):
		_solves(HEAD + f"struct s {{ u8 n; u8 p; u8 q; {body} }}\n")


@pytest.mark.skipif(not fourway.COMPLETE, reason="needs gcc, g++ and rustc")
def test_all_four_read_it_alike(tmp_path: Path) -> None:
	command = _built(tmp_path, PROVED)
	# n = 6, p = 2: three bytes; n = 2, p = 5: none, `max` choosing 6.
	for struct in ("a", "b"):
		assert _all(command, bytes([6, 2, 1, 2, 3, 7]), struct, "d",
		            tmp_path).startswith("len=3"), struct
		assert _all(command, bytes([6, 2, 1, 2, 3, 7]), struct, "last",
		            tmp_path) == "7"
		assert _all(command, bytes([2, 5, 7]), struct, "last",
		            tmp_path) == "7"
