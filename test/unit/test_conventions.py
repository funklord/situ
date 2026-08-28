"""The source conventions of project.md section 25 are checked by the test
suite, not only by `make lint`, so a violation fails CI wherever it enters."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tokenize
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tool"))

import pytest

import python_floor  # noqa: E402
import style_gate  # noqa: E402
from python_floor import declared_floor  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG  = style_gate.load_config(ROOT)


def test_tree_follows_conventions() -> None:
	problems = []
	# `discover` returns the kept list and the raw population it was
	# filtered from; the count exists for the collapse floor and is not
	# this test's business.
	files, _ = style_gate.discover(ROOT, CFG)
	for path in files:
		problems.extend(style_gate.check_file(path, ROOT, CFG))

	assert not problems, "\n" + "\n".join(str(problem) for problem in problems)


def test_linter_flags_space_indent(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int main(void)\n{\n    return 0;\n}\n", encoding="ascii")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert [problem.message for problem in problems] == ["indented 0 tab(s), structure says 1"]


def test_linter_flags_space_before_tab(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int main(void)\n{\n \treturn 0;\n}\n", encoding="ascii")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert "space before tab in indent" in [problem.message for problem in problems]


def test_linter_allows_alignment_after_tab(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("void f(void)\n{\n\tg(a,\n\t  b);\n}\n", encoding="ascii")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_allows_block_comment_continuation(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("/* one\n * two\n */\n", encoding="ascii")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_flags_non_ascii_in_a_c_comment(tmp_path: Path) -> None:
	"""A comment is prose, and prose is ASCII.

	This asserted `non-ASCII byte` until `c_ascii_problems` arrived and gave
	C the same literal exemption Python has. The detection never lapsed --
	only the wording did -- which is the shape of a stale expectation worth
	spelling out: the gate was right and the test was describing an older
	one.
	"""
	source = tmp_path / "sample.c"
	# Spelled as bytes so this file stays ASCII and passes its own check.
	source.write_bytes(b"/* caf\xc3\xa9 */\n")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert len(problems) == 1
	assert problems[0].message == "non-ASCII '\u00e9' outside a literal"
	assert (problems[0].line, problems[0].col) == (1, 7)


def test_linter_allows_non_ascii_inside_a_c_literal(tmp_path: Path) -> None:
	"""A glyph a program prints is output, in C as in Python.

	The rule's own example is `GREEN('gpg ')`, which is C -- so a scanner
	that exempted only Python literals would have left the rule unenforced
	in the language it was written about.
	"""
	source = tmp_path / "sample.c"
	source.write_text('const char *tick = "\u2713";\n', encoding="utf-8")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_allows_non_ascii_inside_a_c_char_literal(
		tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("static const char c = '\u00e9';\n", encoding="utf-8")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_falls_back_to_bytes_when_c_will_not_lex(
		tmp_path: Path) -> None:
	"""An unterminated comment means the scanner lost its place.

	Same contract as the Python side: a file whose literals cannot be
	located is not a file whose prose has been cleared, so the stricter
	whole-file check decides.
	"""
	source = tmp_path / "sample.c"
	source.write_bytes(b"/* caf\xc3\xa9\n")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert len(problems) == 1
	assert problems[0].message.startswith("non-ASCII byte")


def test_linter_allows_non_ascii_inside_a_python_literal(tmp_path: Path) -> None:
	"""A tick a program prints is output, not prose.

	The ASCII rule governs the text the repository writes about itself, and a
	whole-file byte check cannot tell that from what a program emits. The one
	project that prints status ticks switched the check off to keep them,
	which switched it off for its comments too, and an em dash arrived in
	one. Spelled with escapes so this file stays ASCII bytes throughout.
	"""
	source = tmp_path / "sample.py"
	source.write_text('TICK = "\u2713 ok"\n', encoding="utf-8")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_allows_non_ascii_inside_a_python_fstring(tmp_path: Path) -> None:
	"""3.12 splits an f-string into START/MIDDLE/END; 3.11 emits one STRING.

	Both spellings have to allow it, which is why the literal set is built by
	name lookup rather than by naming a token type that 3.11 does not have.
	"""
	source = tmp_path / "sample.py"
	source.write_text('def f(n):\n\treturn f"\u2713 {n}"\n', encoding="utf-8")

	assert style_gate.check_file(source, tmp_path, CFG) == []


def test_linter_flags_non_ascii_in_a_python_comment(tmp_path: Path) -> None:
	source = tmp_path / "sample.py"
	source.write_text("# an em dash \u2014 here\n", encoding="utf-8")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert len(problems) == 1
	assert "outside a string literal" in problems[0].message
	assert (problems[0].line, problems[0].col) == (1, 14)


def test_linter_falls_back_to_bytes_when_python_will_not_tokenise(
		tmp_path: Path) -> None:
	"""A file nobody can parse is not a file that has been cleared.

	The Python path returns None rather than an empty list when tokenize
	gives up, so the caller reaches the stricter whole-file check instead of
	reading the failure as a pass.
	"""
	source = tmp_path / "sample.py"
	source.write_text("X = (\u2014,\n", encoding="utf-8")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert len(problems) == 1
	assert problems[0].message.startswith("non-ASCII byte")


def test_linter_flags_trailing_whitespace(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int x;\t\n", encoding="ascii")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert [problem.message for problem in problems] == ["trailing whitespace"]


def test_linter_flags_missing_final_newline(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int x;", encoding="ascii")

	problems = style_gate.check_file(source, tmp_path, CFG)

	assert [problem.message for problem in problems] == ["no newline at end of file"]


def test_linter_ignores_python_string_content(tmp_path: Path) -> None:
	"""Golden diagnostic texts have a space gutter that is content, not indent."""
	source = tmp_path / "sample.py"
	source.write_text('EXPECTED = """\n   |\n41 | require x;\n   | ^^^^^^^^^^\n"""\n',
	                  encoding="ascii")

	assert style_gate.check_file(source, tmp_path, CFG) == []


# -- the repository layout section (23) -------------------------------------


def test_the_layout_section_lists_every_compiler_module() -> None:
	"""Section 23 is a map of the tree, and `unparse.py` was missing from it.

	A map with a module absent is worse than no map: a reader looking for
	where an expression becomes text again concludes there is nowhere, which
	is how a sixth copy of something gets written.
	"""
	text    = (ROOT / "project.md").read_text(encoding="utf-8")
	section = text[text.index("## 23. Repository layout"):text.index("### 23.1")]
	block   = section[section.index("  situc/"):]
	# To the next entry at exactly two spaces. Slicing at `"\n  "` cut at the
	# first *four*-space line instead, because two spaces is a prefix of four,
	# and the block came out one line long.
	end     = re.search(r"\n  (?=\S)", block[1:])
	block   = block if end is None else block[:end.end()]

	listed = set(re.findall(r"^\s{4}([a-z_0-9]+\.py)\b", block, re.M))
	real   = {path.name for path in (ROOT / "situc").glob("*.py")}

	assert listed == real


# -- the failure classes, across four runtimes ------------------------------


def failure_classes() -> set[str]:
	"""The classes `situ_err_t` names, lowercased and unprefixed."""
	header = (ROOT / "runtime" / "c" / "situ.h").read_text(encoding="utf-8")
	body   = header.split("} situ_err_t;")[0]
	return {name.lower()
	        for name in re.findall(r"SITU_ERR_(\w+)\b", body)}


def test_the_failure_classes_match_the_runtimes() -> None:
	"""One list, four runtimes, and section 20.2 claiming to name it.

	A class C can report and Rust cannot is a condition a Rust consumer has no
	way to express. `truncated` was added to all four by hand, and the fact
	that nothing would have caught a fourth omission is the reason this
	exists. Section 20.2 had listed five of the seven -- `stale` had been
	missing since long before `truncated` was.
	"""
	classes = failure_classes()
	assert classes, "the C enum parses"

	spec = (ROOT / "project.md").read_text(encoding="utf-8")
	line = next(one for one in spec.splitlines()
	            if "distinct codes per failure class" in one)
	listed = spec[spec.index(line):spec.index(line) + 400]
	for name in classes:
		assert name in listed, f"section 20.2 does not name `{name}`"

	# C++ shares the values outright; the other two spell their own.
	cpp    = (ROOT / "runtime" / "cpp" / "situ.hpp").read_text(encoding="utf-8")
	rust   = (ROOT / "runtime" / "rust" / "situ_rt.rs").read_text(encoding="utf-8")
	python = (ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
		encoding="utf-8")

	# Rust has no `Stale`: invalidation there is the borrow checker, so a view
	# outliving a layout-shifting write does not compile and there is no
	# run-time condition to name (26.18). The one exemption, and it is here
	# rather than absent so that a second one has to be argued for.
	compiled_away = {"rust": {"stale"}}

	for name in classes:
		assert f"= SITU_ERR_{name.upper()}" in cpp, f"C++ lacks `{name}`"
		if name not in compiled_away["rust"]:
			assert re.search(rf"^\t{name.capitalize()},", rust, re.M), \
				f"Rust lacks `{name}`"
		# Python raises rather than returning, so a class is an exception
		# type -- `StaleViewError` for `stale`, and so on.
		assert re.search(rf"^class {name.capitalize()}\w*Error", python, re.M), \
			f"Python lacks `{name}`"


# -- the interpreter floor --------------------------------------------------


def test_section_22_and_pyproject_agree_on_the_floor() -> None:
	"""Two statements of one number, and the section is the one people read."""
	spec = (ROOT / "project.md").read_text(encoding="utf-8")

	assert f"Python {declared_floor()}+" in spec


#: Known-bad and known-good f-strings, the first three legal only from 3.12.
#: The good half is the half that matters: a detector that refuses valid code
#: is worse than the silence it replaces, and three of the four are shapes a
#: naive version gets wrong -- a *different* quote inside the expression, a
#: backslash in the *literal* part, and triple quotes, which may span lines in
#: every version.
_PEP_701_BAD = [
	('x = f"{d["key"]}"\n',        "a string reusing"),
	('x = f"{a +\n     b}"\n',     "split across lines"),
	("x = f\"{'\\n'.join(y)}\"\n", "a backslash inside"),
]
_PEP_701_GOOD = [
	"x = f\"{d['key']}\"\n",
	'x = f"a\\nb {v}"\n',
	'x = f"""\n{v}\n"""\n',
	'x = f"{a}" f"{b}"\n',
]


def test_below_floor_catches_grammar_as_well_as_tokens() -> None:
	"""`below_floor` is two instruments and each is blind where the other sees.

	The f-string control below exercises the tokenizer half. This is the other:
	`ast.parse(feature_version=...)` sees grammar added since the floor, which
	is most of what a new version brings and none of what PEP 701 did. A
	control over one half would leave the other able to break silently, which
	is the mistake the C++ standards work made one language over -- a tool that
	inspects one thing answers for that thing only.

	`match` is the case that keeps this honest: it arrived in 3.10, so it is
	*below* the floor and must pass. A check keyed on "looks modern" fails it.
	"""
	for source in ("type X = int\n", "def f[T](x: T) -> T:\n\treturn x\n"):
		assert python_floor.below_floor(source) != [], source

	for source in ("match x:\n\tcase 1:\n\t\tpass\n", "x = 1\n"):
		assert python_floor.below_floor(source) == [], source


def test_the_f_string_detector_finds_what_it_claims_to() -> None:
	"""The control, and the reason to trust the checks' silence.

	Every module in this tree is clean, so they pass -- and would pass just
	as loudly if the detector were broken. These samples separate the two.
	"""
	if python_floor.FSTRING_START is None:
		pytest.skip("running on 3.11, where the interpreter is the check")

	for source, expected in _PEP_701_BAD:
		found = python_floor.pep_701(source)
		assert any(expected in why for _, why in found), \
			f"missed {expected!r} in {source!r}"

	for source in _PEP_701_GOOD:
		assert python_floor.pep_701(source) == [], f"false positive on {source!r}"


def test_no_module_uses_a_construct_the_floor_cannot_parse() -> None:
	"""The floor check that runs on the machine the code is written on.

	`test_every_module_parses_at_the_declared_floor` below is stronger and
	runs a real interpreter -- where one is installed. It was not installed
	here, and that is the condition its own docstring records as having cost
	six phases: "the machine this was written on runs 3.13 and every test
	passed there". A guard that only fires somewhere else did not fire.

	So this one never skips below a 3.12 floor. It cannot replace running
	3.11 -- `below_floor` knows the grammar plus three tokenizer changes
	where the interpreter knows the language -- but it closes the gap that
	actually opened, and closes it where the code is authored.
	"""
	if python_floor.floor_version() >= (3, 12):
		pytest.skip(f"the floor is {declared_floor()}, where PEP 701 is available")

	failed = []
	for path in python_floor.shipped_modules():
		for line, why in python_floor.below_floor(path.read_text(encoding="utf-8")):
			failed.append(f"{path.relative_to(ROOT)}:{line}: {why}")

	assert failed == [], (
		f"Python {declared_floor()} is the declared floor and cannot parse these:\n  "
		+ "\n  ".join(failed))


def test_every_module_parses_at_the_declared_floor() -> None:
	"""An interpreter of the declared version, over every module here.

	`situc` claims Python 3.11 and had not run on it for six phases: one f-string
	in the C++ backend split an expression across lines, which is PEP 701 and
	therefore 3.12. Nothing noticed, because the machine this was written on runs
	3.13 and every test passed there.

	`ast.parse(feature_version=...)` does not catch it -- PEP 701 is a tokenizer
	change and the flag does not reach the tokenizer -- so this runs the real
	interpreter, and skips where that version is not installed. The claim is
	worth a check that sometimes skips: it is the difference between vendoring
	into an embedded build environment and not.
	"""
	floor  = declared_floor()
	python = shutil.which(f"python{floor}")
	if python is None:
		pytest.skip(f"no python{floor} on PATH")

	modules = python_floor.shipped_modules()

	failed = []
	for path in modules:
		result = subprocess.run(
			[python, "-c", "import ast, sys; ast.parse(open(sys.argv[1]).read())",
			 str(path)],
			capture_output=True, text=True)
		if result.returncode != 0:
			failed.append(f"{path.relative_to(ROOT)}: "
			              f"{result.stderr.strip().splitlines()[-1]}")

	assert not failed, (
		f"these do not parse on python{floor}, which section 22 and "
		f"pyproject.toml both promise:\n  " + "\n  ".join(failed))


# -- the generated-C build's own list of schemas -----------------------------


def test_the_stuffing_code_list_has_one_home() -> None:
	"""Which stuffing codes generate is one list, and it has been three.

	`codegen/c/derived.py` carries a note reading "one list because there
	were two" -- the dispatch and the prototype gate, consolidated after
	adding a code to one of them emitted a definition nothing declared. That
	pass did not find a third copy in `traverse.py`, which is the one every
	backend asks the *shape* question of: whether a region's decode has a
	settled interface to call.

	It cost exactly what the first two cost. Adding `slip` and `ppp_async` to
	the generator's copy left `traverse` at three codes, so every backend was
	told a SLIP region had no settled decode and declined an accessor all
	four could have emitted -- the C runtime function existed, and Rust's
	safe wrapper around it existed, and nothing called either.

	Nothing about that is visible in a diff of the file being edited, which
	is why it is asserted rather than remembered.
	"""
	defined = sorted(
		path.relative_to(ROOT)
		for path in ROOT.glob("situc/**/*.py")
		if re.search(r"^DERIVED_STUFFING\s*=", path.read_text(encoding="ascii"),
		             re.MULTILINE))

	assert len(defined) == 1, (
		f"DERIVED_STUFFING is defined in {defined}; two lists of which codes "
		f"generate is how one of them goes stale without a symptom")

	from situc.codegen.c import derived
	from situc import traverse
	assert derived.DERIVED_STUFFING is traverse.DERIVED_STUFFING, (
		"the generator does not read the list it is defined in")


def test_the_generated_build_lists_every_schema() -> None:
	"""`test/generated/Makefile` names the schemas it builds, by hand.

	It is the third place in this repository that answers "which schemas are
	there" -- `test/unit/every_schema.py` and the two agreement checks are
	the others -- and the only one nothing held to the tree. That is the shape
	that let `test/schema/edges.situ` go unbuilt in C++ for weeks: a list
	somebody has to remember to extend, extended by somebody who did not.

	The cost of forgetting here is larger than a compile check. Every schema
	in that list gets a capability-conformance suite, a fuzz harness and a
	compiled object, so one left out is a schema whose generated C nothing in
	the build ever runs.
	"""
	block = (ROOT / "test" / "generated" / "Makefile").read_text(
		encoding="utf-8").split("SCHEMAS\t\t:=", 1)[1].split("\nGEN_NAMES", 1)[0]
	listed = set(re.findall(r"[\w./]+\.situ", block))

	real = {str(path.relative_to(ROOT))
	        for path in [*ROOT.glob("example/*/*.situ"),
	                     *ROOT.glob("test/schema/*.situ"),
	                     *ROOT.glob("std/*.situ")]}

	# `std/codecs.situ` declares codec signatures and nothing else: it has no
	# struct, so its generated C is an include guard and a comment, and there
	# is nothing for a check suite or a fuzz harness to reach. `kernels.situ`
	# beside it carries the derived-codec path that does generate code.
	exempt = {"std/codecs.situ"}

	assert listed | exempt == real | exempt, (
		f"in the tree and not built: {sorted(real - listed - exempt)}; "
		f"built and not in the tree: {sorted(listed - real)}")
