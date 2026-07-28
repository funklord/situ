"""docs/grammar.ebnf says it is "extracted from project.md section 7 and kept
in sync with it". Until this file, the only thing keeping that true was
somebody remembering, and the commit that added the `invariant` production
updated section 7, wrote that the two are held together by habit, and forgot
the extracted copy in the same breath.

The direction is one-way. Section 7 is authoritative, so every production it
declares must appear in the extracted file; the extracted file may declare
more, because it also lists the productions described elsewhere in project.md
that section 7 has not absorbed yet.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# A production is a name at the left margin followed by `=`. Alternatives are
# indented, so anchoring at column zero is what separates the two.
PRODUCTION = re.compile(r"^([a-z_][a-z0-9_]*)\s*=", re.MULTILINE)


def section_7() -> str:
	text  = (ROOT / "project.md").read_text(encoding="ascii")
	start = text.index("## 7.")
	body  = text[start:text.index("## 8.", start)]
	return body[body.index("```ebnf") + len("```ebnf"):body.index("```", body.index("```ebnf") + 3)]


def test_every_production_in_section_7_is_in_the_extracted_grammar() -> None:
	declared  = set(PRODUCTION.findall(section_7()))
	extracted = set(PRODUCTION.findall(
		(ROOT / "docs/grammar.ebnf").read_text(encoding="ascii")))

	assert declared, "section 7's grammar block did not parse"
	assert declared <= extracted, (
		"project.md section 7 declares productions docs/grammar.ebnf does not: "
		f"{sorted(declared - extracted)}")


def test_the_extracted_grammar_says_which_one_wins() -> None:
	"""Two copies of a grammar disagree eventually. The file is only safe to
	keep if a reader knows which one is the bug."""
	header = (ROOT / "docs/grammar.ebnf").read_text(encoding="ascii")[:800]

	assert "project.md is authoritative" in header
