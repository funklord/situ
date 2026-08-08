"""The source conventions of project.md section 25 are checked by the test
suite, not only by `make lint`, so a violation fails CI wherever it enters."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import pytest

import style_gate  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CFG  = style_gate.load_config(ROOT)


def test_tree_follows_conventions() -> None:
	problems = []
	for path in style_gate.discover(ROOT, CFG):
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


def test_linter_flags_non_ascii(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	# Spelled as bytes so this file stays ASCII and passes its own check.
	source.write_bytes(b"/* caf\xc3\xa9 */\n")

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


def declared_floor() -> str:
	"""The oldest Python this compiler claims to run on, from the one place
	that states it as data: mypy's `python_version` in `pyproject.toml`."""
	text  = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
	found = re.search(r'^python_version\s*=\s*"(\d+\.\d+)"', text, re.M)
	assert found is not None, "pyproject.toml no longer declares python_version"
	return found.group(1)


def test_section_22_and_pyproject_agree_on_the_floor() -> None:
	"""Two statements of one number, and the section is the one people read."""
	spec = (ROOT / "project.md").read_text(encoding="utf-8")

	assert f"Python {declared_floor()}+" in spec


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

	modules = [ROOT / "bin" / "situc",
	           *sorted(ROOT.glob("situc/**/*.py")),
	           *sorted(ROOT.glob("tools/*.py"))]

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


def test_the_generated_build_lists_every_schema() -> None:
	"""`tests/generated/Makefile` names the schemas it builds, by hand.

	It is the third place in this repository that answers "which schemas are
	there" -- `tests/unit/every_schema.py` and the two agreement checks are
	the others -- and the only one nothing held to the tree. That is the shape
	that let `tests/schemas/edges.situ` go unbuilt in C++ for weeks: a list
	somebody has to remember to extend, extended by somebody who did not.

	The cost of forgetting here is larger than a compile check. Every schema
	in that list gets a capability-conformance suite, a fuzz harness and a
	compiled object, so one left out is a schema whose generated C nothing in
	the build ever runs.
	"""
	block = (ROOT / "tests" / "generated" / "Makefile").read_text(
		encoding="utf-8").split("SCHEMAS\t\t:=", 1)[1].split("\nGEN_NAMES", 1)[0]
	listed = set(re.findall(r"[\w./]+\.situ", block))

	real = {str(path.relative_to(ROOT))
	        for path in [*ROOT.glob("examples/*/*.situ"),
	                     *ROOT.glob("tests/schemas/*.situ"),
	                     *ROOT.glob("std/*.situ")]}

	# `std/codecs.situ` declares codec signatures and nothing else: it has no
	# struct, so its generated C is an include guard and a comment, and there
	# is nothing for a check suite or a fuzz harness to reach. `kernels.situ`
	# beside it carries the derived-codec path that does generate code.
	exempt = {"std/codecs.situ"}

	assert listed | exempt == real | exempt, (
		f"in the tree and not built: {sorted(real - listed - exempt)}; "
		f"built and not in the tree: {sorted(listed - real)}")
