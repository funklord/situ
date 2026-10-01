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

import pytest

from every_schema import SCHEMAS

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


def test_the_grammar_names_every_member_keyword_the_parser_dispatches_on(
		) -> None:
	"""The population the test above cannot see.

	Its name says "every spelling the parser accepts" and its population
	is every member of an `ast` enum -- true of every enum value, and
	silent about a keyword that is not one. `preamble` is exactly that:
	`parse_member` branches on the literal text and builds an
	`ast.Reserved`, so no enum carries the word, and it appeared nowhere
	in either grammar's 341 lines while `example/png` used it for the
	eight-byte PNG signature (26.472).

	`evidence.md` calls this a name that claims exhaustiveness over a
	hand-written enumeration. The quantifier is what needed checking, not
	the assertion under it -- so this derives its list from the PARSER's
	own dispatch rather than from a second enumeration, and a keyword
	added there is in this population the day it is added.
	"""
	import re

	source = (ROOT / "situc/parser.py").read_text(encoding="ascii")
	grammar = (ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")

	# Every literal the parser compares a token's text against. Wider than
	# member keywords alone, which is the point: a spelling is a spelling.
	spellings = sorted(set(re.findall(r'token\.text == "([a-z_]+)"', source)))
	assert len(spellings) > 15, (
		f"only {len(spellings)} spellings found; the dispatch has been "
		f"rewritten and this is reading the wrong thing")

	missing = [word for word in spellings if f'"{word}"' not in grammar]
	assert not missing, (
		"the parser dispatches on these and doc/grammar.ebnf names none of "
		"them: " + ", ".join(missing))


def test_the_extracted_grammar_says_which_one_wins() -> None:
	"""Two copies of a grammar disagree eventually. The file is only safe to
	keep if a reader knows which one is the bug."""
	header = (ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")[:800]

	assert "project.md is authoritative" in header


#: Schemas the grammar must NOT derive. A recogniser that accepts everything
#: reports a clean grammar exactly as loudly as a correct one, and this file
#: made the grammar more permissive nine times to reach 42 of 42 -- so the
#: cases that must still be refused are part of the instrument rather than a
#: nicety. Each is one edit away from a schema that does derive.
MALFORMED = {
	"a missing semicolon":       "struct s { u8 a }\n",
	"an unclosed brace":         "struct s { u8 a;\n",
	"a word that is not a decl": "wibble w;\n",
	"two type names":            "struct s { u8 u16 a; }\n",
	"a cap with no expression":  "struct s { u8 c[remaining] max; }\n",
	"covers without parens":     "struct s { tag u8 t[16] covers body; }\n",
	"an enum with no backing":   "enum e { a = 1, }\n",
	"a stray comma":             "struct s { u8 a,; }\n",
	"`default` with no colon":   "struct s { u8 k;\n"
	                             " variant v switch (k) { default error; } }\n",
	"a trailing alternative":    'struct s { u8 a[] until "x" | ; }\n',
}

PREAMBLE = "target buffer;\nendian big;\n"


def _grammar() -> dict[str, object]:
	from ebnf import parse_ebnf
	return parse_ebnf((ROOT / "doc/grammar.ebnf").read_text(encoding="ascii"))


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.stem)
def test_the_grammar_derives_every_schema_in_the_corpus(path: Path) -> None:
	"""The witness the other four could not be (26.542, 26.543).

	Two of them compare `doc/grammar.ebnf` against project.md section 7, so
	they are satisfied whenever the two copies agree -- and they agreed while
	both were wrong. The other two derive a keyword population from the
	parser, which is the deliberate third witness, and both are keyed on the
	SPELLING: `sealed ... until` and `[remaining] max N` were an existing
	spelling in a NEW POSITION, so nothing was missing from the vocabulary
	and no vocabulary check could speak.

	This asks the only question that separates the grammar from the parser:
	can the grammar derive the schemas this repository builds.
	`test/schema/edges.situ` carries every construct the worked examples
	happen not to have, by policy, so a construct the grammar cannot derive
	fails here the day it is added.

	It found nine gaps on its first run, 20 of 42 schemas being
	underivable -- among them `impl` and `varint_type` declarations, which
	the grammar DEFINED and `decl` did not reach, and `ones_complement`,
	which is an `ast.KernelFamily` member that `ENUMERATED` above does not
	list. A production that exists is not a production that is reachable.

	**What it does not prove**, pinned here rather than left to be assumed:
	that the grammar refuses everything outside the language. It is a
	derivability check, and a grammar loosened far enough passes it --
	`MALFORMED` is what holds that line. And its reach is the corpus's
	reach: a construct no schema here uses is one it cannot see, which is
	`pad_to(4) [attrs];` today, read out of `situc/parser.py` rather than
	proved by any schema.
	"""
	from ebnf import recognise

	verdict = recognise(path, _grammar())
	assert verdict.ok, (
		f"doc/grammar.ebnf cannot derive {path.name}: {verdict.where()}")


def test_the_recogniser_refuses_a_malformed_schema() -> None:
	"""The control, and it is not optional.

	Nine of the productions above were widened to make the corpus derive.
	A tenth widening could have been `schema = { ? any ? }`, which would
	pass every case in the test above and mean nothing. These are the cases
	that must still be refused -- so the derivability result is a statement
	about the grammar rather than about how permissive it was made.
	"""
	import tempfile

	from ebnf import recognise

	grammar = _grammar()
	accepted = []
	for name, body in MALFORMED.items():
		with tempfile.NamedTemporaryFile("w", suffix=".situ",
		                                 delete=False) as handle:
			handle.write(PREAMBLE + body)
			path = Path(handle.name)
		try:
			if recognise(path, grammar).ok:
				accepted.append(name)
		finally:
			path.unlink()

	assert not accepted, ("the grammar derives schemas it should refuse, so "
	                      "the derivability check above proves nothing: "
	                      + ", ".join(accepted))


def test_every_undefined_name_in_the_grammar_is_one_the_recogniser_handles(
		) -> None:
	"""The population, before either result above is believed.

	An unmatchable terminal makes every production above it unmatchable, so a
	name the recogniser does not handle turns a clean grammar into a broken
	corpus -- and the failure reads as a schema the grammar cannot derive,
	which is the wrong finding entirely.

	`uint = "u" digits` is why this is asserted rather than assumed. The
	grammar is a CHARACTER grammar throughout and the recogniser works on
	tokens, so every lexical production needs overriding; that one was
	missed, and a field parsed anyway because `type_ref = scalar_type | ident`
	falls through to `ident`. Only `tag_field`, which names `scalar_type`
	directly, could not match `u8` -- so the mismatch was invisible on the
	construct it appears in most and fatal on one it appears in once.
	"""
	from ebnf import BY_TOKEN, UNDEFINED, parse_ebnf

	text    = (ROOT / "doc/grammar.ebnf").read_text(encoding="ascii")
	grammar = parse_ebnf(text)
	rules   = _productions(text)

	used: set[str] = set()
	for rhs in rules.values():
		bare = re.sub(r'"[^"]*"|\?[^?]*\?', " ", rhs)
		used |= set(re.findall(r"[a-z_][a-z0-9_]*", bare))

	undefined = used - set(grammar)
	assert undefined == set(UNDEFINED), (
		f"the grammar's undefined names have moved: {sorted(undefined)}, "
		f"where ebnf.UNDEFINED says {sorted(UNDEFINED)}")

	# `letter` and `digit` are the two the recogniser does not match, and it
	# does not need to: they are reachable only from `ident` and `digits`,
	# which it overrides as whole tokens. That reachability is the actual
	# guarantee, so it is checked rather than stated.
	handled = set(BY_TOKEN) | {"expr"}
	for name in sorted(undefined - handled):
		callers = {rule for rule, rhs in rules.items()
		           if re.search(rf"\b{name}\b",
		                        re.sub(r'"[^"]*"', " ", rhs))}
		assert callers <= set(BY_TOKEN), (
			f"`{name}` is unmatched and reachable from {sorted(callers)}, "
			"which the recogniser does not override")
