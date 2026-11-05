"""`[n]` on a text number counts digits, and a field cannot give that count.

`decimal u16 code[n]` was accepted with four answers: C parsed a span of zero,
three backends dropped the number, and the walkers took two bytes a digit.
Nothing in the corpus had the shape, so nothing compared them (26.880).
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text

HEAD = "target buffer;\nendian big;\n"


@pytest.mark.parametrize("body", [
	"struct g { u8 n; decimal u16 code[n]; }",
	"struct g { u8 n; decimal u8 code[n]; }",
	"struct g { u8 n; decimal u8 code[n + 1]; }",
	"struct g { u8 n; hex u16 code[n]; }",
])
def test_a_width_the_message_gives_is_refused(body: str) -> None:
	with pytest.raises(SituError) as caught:
		solve(parse_text(HEAD + body, path="s.situ"))
	said = caught.value.diagnostic.render()
	assert "takes its width from the message" in said
	assert "`until" in said


@pytest.mark.parametrize("body", [
	"struct g { decimal u16 code[3]; }",
	"const W = 4;\nstruct g { decimal u32 code[W]; }",
	"struct g { decimal u32 code until \":\"; }",
	"struct g { decimal u32 n[4]; u8 d[n]; }",
])
def test_the_spellings_it_recommends_stand(body: str) -> None:
	"""The control: a written width, a constant, and a delimiter all work,
	and a text number may still size something else."""
	solve(parse_text(HEAD + body, path="s.situ"))
