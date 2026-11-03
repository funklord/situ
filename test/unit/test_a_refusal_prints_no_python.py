"""No diagnostic shows the reader a Python object (26.851).

A note in the scaled-fixed-width refusal interpolated an AST node into an
f-string -- `f"[{member.array.size}]"` -- and an f-string over a node gives
its REPR. The node carries a `Span`, the span carries its `Source`, and the
source carries the whole file, so a sentence suggesting a better spelling
printed the entire schema back at its author:

    `decimal i64 code[IntLiteral(span=Span(source=Source(path='...',
    text='target buffer;\\nendian big;\\n...'), start=109, end=110),
    value=5, text='5')]` reads the digits

Section 17 says what a diagnostic is for, and this is the same class as
26.485 -- Python objects reaching shipped output -- arriving in a message
rather than in generated source.

**The detector is the MESSAGE, not the source.** A static sweep for an
f-string over a node cannot tell `{kind.value}` (an enum's string, which is
right) from `{member.array.size}` (a node), and there are a dozen of the
first. So this renders real refusals and reads what a person would see,
which is also what makes it extensible: a new refusal family joins `BAD`
and is covered without anybody writing a rule.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.diagnostics import SituError			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HEAD = "target buffer;\nendian big;\nbit_order msb_first;\n\n"

#: Fragments the front end refuses, one per family reachable from here. Each
#: is asserted to refuse, so a fragment that becomes LEGAL fails this rather
#: than quietly leaving the sweep with one case fewer -- which is the vacuous
#: pass this file would otherwise be one language change away from.
BAD = (
	"struct s { u8 kind; variant b switch (kind) {"
	" case 1: scaled i64 c[5]; default: u8 o; } }",
	"struct s { scaled i64 c[5]; }",
	"struct s { decimal i64 c[5]; }",
	"struct s { u8 n; u8 d[n] [must_eq = 3]; }",
	"struct s { u8 a; pad_to(4) [must_be_one]; }",
	"struct s { u8 kind; variant b switch (kind) {"
	" case 1: reserved u4; default: u8 o; } }",
	"struct s { u8 a; } invariant s.a == size(s.a + s.a);",
	"struct s { u8 a [max = 255]; u8 b[a[-1].a]; }",
	"struct s { u8 a; u8 b[a] [encoding = klingon]; }",
	"struct s { u8 a; u8 arg_n; parameter u8 n; }",
	"struct s { opaque u8 x; }",
)

#: What a reader must never be shown. `object at 0x` catches anything with
#: the default `__repr__`; the three named classes catch the dataclasses
#: whose reprs are recursive enough to print a whole file.
PYTHON = re.compile(r"Span\(|Source\(|Literal\(|object at 0x|<situc\.")


def _refusal(body: str) -> str:
	text = HEAD + body + "\n"
	try:
		parsed = parse_text(text)
		resolve(parsed, solve(parsed))
	except SituError as why:
		notes = list(getattr(why.diagnostic, "notes", None) or [])
		labels = [one.text for one in getattr(why.diagnostic, "labels", None)
		          or [] if getattr(one, "text", None)]
		return "\n".join([str(why), *notes, *labels])
	raise AssertionError(f"accepted, so it tests nothing: {body}")


@pytest.mark.parametrize("body", BAD, ids=range(len(BAD)))
def test_a_refusal_shows_no_python_object(body: str) -> None:
	rendered = _refusal(body)
	found = PYTHON.search(rendered)
	assert found is None, (
		f"`{found.group(0) if found else ''}` in a message a person reads:"
		f"\n{rendered[:600]}")


def test_the_note_that_found_this_names_the_width_as_written() -> None:
	"""The case itself, asserted positively rather than only by absence.

	A sweep that only forbids reprs passes against a note that dropped the
	suggestion entirely, so this pins what the sentence has to SAY: the
	width as the author wrote it, inside the spelling being suggested.
	"""
	rendered = _refusal("struct s { scaled i64 code[5]; }")
	assert "`decimal i64 code[5]` reads the digits" in rendered, rendered
