"""doc/grammar.ebnf says it is "extracted from project.md section 7 and kept
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
		(ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")))

	assert declared, "section 7's grammar block did not parse"
	assert declared <= extracted, (
		"project.md section 7 declares productions doc/grammar.ebnf does not: "
		f"{sorted(declared - extracted)}")


def _productions(src: str) -> dict[str, str]:
	"""Every production in an EBNF block, as name -> right-hand side.

	Comments go first, because they wrap differently in the two files and
	one of them aligns with tabs -- so whitespace is normalised after, and
	what is left is the grammar rather than its typesetting. A production
	runs from a name at column zero to the `;` that closes it.
	"""
	src = re.sub(r"\(\*.*?\*\)", " ", src, flags=re.S)
	return {m.group(1): re.sub(r"\s+", " ", m.group(2)).strip()
	        for m in re.finditer(r"^([a-z_][a-z0-9_]*)\s*=(.*?);\s*$",
	                             src, re.M | re.S)}


def test_the_two_grammars_agree_about_every_shared_production() -> None:
	"""The axis the containment check above cannot see.

	`test_every_production_in_section_7_is_in_the_extracted_grammar` compares
	NAMES, so a production can be in both files and say different things in
	each -- and did. Measured when this was written: five of the shared
	productions had drifted, every one of them in the direction of section 7
	trailing the parser, for constructs that are built and shipped.

	    decl        no namespace, tokens, endian_marker or register_block
	    member      no marker_field
	    field       no `peek` (0057) and no `at` (0042)
	    radix       no `scaled` (0056)
	    codec_prop  no `kernel =`, which is how a derived codec is declared

	Section 7 is the authoritative one, so each of those was a false
	statement about the language in the document that defines it:
	`radix = "decimal" | "hex"` says those are the radixes, and there are
	three.

	This is the file's own concern one level down. Its header says two
	documents agreeing are one witness if the same hand wrote both, and adds
	the enums as a third -- but an enum only covers a spelling that IS an
	enum member. `peek`, `at` and `kernel =` are none of them, so nothing
	held them.

	The asymmetry stays deliberate: `doc/grammar.ebnf` may declare
	productions section 7 has not absorbed, and does -- sixteen of them. What
	this refuses is the two files declaring one production and disagreeing
	about it.
	"""
	declared  = _productions(section_7())
	extracted = _productions(
		(ROOT / "doc/grammar.ebnf").read_text(encoding="ascii"))

	assert declared, "section 7's grammar block did not parse"
	assert extracted, "doc/grammar.ebnf did not parse"

	shared = sorted(set(declared) & set(extracted))
	assert len(shared) >= 60, (
		f"only {len(shared)} productions are in both files; the extractor "
		"has probably stopped matching one of them")

	differ = {name: (declared[name], extracted[name])
	          for name in shared if declared[name] != extracted[name]}
	assert not differ, (
		"section 7 and doc/grammar.ebnf declare the same production and "
		"disagree about it: "
		+ "; ".join(f"{name}: section 7 says {a!r}, the extracted grammar "
		            f"says {b!r}" for name, (a, b) in differ.items()))


#: The enums whose members are surface keywords inside an *enumerated*
#: production. `attr = ident [ "=" expr ]` is deliberately generic, so the
#: attribute vocabulary is not here -- the grammar does not claim to list it,
#: and `wellformed.py` is what checks a name is one situ knows.
#:
#: Two spellings are not the member's value, and each is named rather than
#: skipped by a rule: an enum member that stops being surface syntax should
#: have to be added here, not silently pass a filter.
SPELLED_DIFFERENTLY = {
	"add":  '"+" digits',          # `expansion = +16`
	"none": '[ "not" ] "seekable"',  # `not seekable`, not `seekable = none`
}

ENUMERATED = ("TargetKind", "Granularity", "Seekable", "Expansion",
              "Severity")


def test_the_grammar_names_every_spelling_the_parser_accepts() -> None:
	"""The third witness, and the one that was missing.

	`doc/grammar.ebnf` is checked against `project.md` section 7 and section 7
	against nothing. Both are documents, and they agreed with each other while
	both trailed the parser: `file`, `append`, `pad_to`, `tag_bytes`,
	`nonce_bytes`, `max_bytes`, `systematic`, `error_propagating` and
	`ratio_padded` were accepted by the compiler and named in neither.

	Two documents agreeing are one witness if the same hand wrote both. The
	code is the other hand: these enums are what the parser turns source text
	into, so every member of them is a spelling somebody can type.
	"""
	from situc import ast

	grammar = (ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")
	missing = []
	for name in ENUMERATED:
		for member in getattr(ast, name):
			value = member.value
			if value in SPELLED_DIFFERENTLY:
				assert SPELLED_DIFFERENTLY[value] in grammar, (
					f"{name}.{member.name} is spelled "
					f"{SPELLED_DIFFERENTLY[value]}, which the grammar drops")
				continue
			if f'"{value}"' not in grammar:
				missing.append(f"{name}.{member.name} (`{value}`)")

	assert not missing, (
		"doc/grammar.ebnf names no spelling for: " + ", ".join(missing))


def test_the_extracted_grammar_says_which_one_wins() -> None:
	"""Two copies of a grammar disagree eventually. The file is only safe to
	keep if a reader knows which one is the bug."""
	header = (ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")[:800]

	assert "project.md is authoritative" in header
