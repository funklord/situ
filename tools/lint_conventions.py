"""Enforce the source conventions of project.md section 25.

There is no autoformatter in this project: black and ruff-format rewrite tabs
to spaces unconditionally, which contradicts the tab-indent rule. This script
is the enforcement mechanism instead. See
docs/decisions/0003-source-formatting.md.
"""

from __future__ import annotations

import sys
import tokenize
from pathlib import Path

# Indentation is checked where we control the whole file. Markdown is excluded:
# its list continuation and code-fence indentation is space-based by
# specification, and fighting that would make the documents wrong.
#
# C++ and Rust were missing from this set for as long as there have been C++
# and Rust backends, so `runtime/cpp/situ.hpp` and `runtime/rust/situ_rt.rs`
# were held to the convention by nothing but the habits of whoever edited
# them. They happened to be clean, which is not the same as being checked.
INDENT_SUFFIXES = frozenset({
	".py", ".c", ".h", ".cpp", ".hpp", ".rs", ".situ", ".ebnf", ".lua",
})
# `bin/situc` is Python without the suffix, because it is a command rather
# than a module. The conventions still apply to it.
INDENT_NAMES = frozenset({"Makefile", "situc"})

# Capability maps are generated, and indented with spaces so their columns line
# up; they get the ASCII and whitespace checks but not the tab rule.
TEXT_SUFFIXES = INDENT_SUFFIXES | frozenset({
	".md", ".json", ".toml", ".cfg", ".clangd", ".map",
})

# Build products and caches. Dot-directories are skipped wholesale rather than
# named one at a time: they hold tool state, never project sources, and a new
# tool should not be able to break the lint by arriving.
SKIP_DIRS = frozenset({"build", "__pycache__"})


class Problem:
	"""One convention violation, rendered in the section 17 location style."""

	def __init__(self, path: Path, line: int, col: int, message: str) -> None:
		self.path    = path
		self.line    = line
		self.col     = col
		self.message = message

	def __str__(self) -> str:
		return f"{self.path}:{self.line}:{self.col}: {self.message}"


def wants_indent_check(path: Path) -> bool:
	return path.suffix in INDENT_SUFFIXES or path.name in INDENT_NAMES


def wants_text_check(path: Path) -> bool:
	return path.suffix in TEXT_SUFFIXES or path.name in INDENT_NAMES


def leading_whitespace(line: str) -> str:
	return line[: len(line) - len(line.lstrip(" \t"))]


def is_python(path: Path) -> bool:
	"""Python by suffix, or by shebang for a command that has no suffix."""
	if path.suffix == ".py":
		return True
	try:
		with path.open("rb") as handle:
			first = handle.readline(64)
	except OSError:
		return False
	return first.startswith(b"#!") and b"python" in first


def literal_lines(path: Path) -> frozenset[int]:
	"""Lines inside multi-line Python string literals.

	Their leading whitespace is content, not indentation: the golden diagnostic
	texts of section 17 have a space gutter that the tab rule must not touch.
	"""
	if not is_python(path):
		return frozenset()

	covered: set[int] = set()
	try:
		with path.open("rb") as handle:
			for token in tokenize.tokenize(handle.readline):
				if token.type == tokenize.STRING and token.end[0] > token.start[0]:
					covered.update(range(token.start[0] + 1, token.end[0] + 1))
	except (tokenize.TokenError, SyntaxError):
		# A file that does not tokenise has a real error the test suite will
		# report; do not also fail it on indentation.
		return frozenset()

	return frozenset(covered)


def is_comment_continuation(line: str) -> bool:
	"""A C block-comment body line, whose leading space aligns the asterisk."""
	return line.lstrip().startswith("*")


def check_text(text: str, where: Path, indent: bool = True,
		skip: frozenset[int] = frozenset()) -> list[Problem]:
	"""The line rules, against text that need not be a file on disk.

	Split out because the code this compiler *emits* was held to the
	conventions by nobody. The tab rule reaches a file when the file has one
	of the suffixes above; generated C, C++, Rust and Lua have those suffixes
	only after they are written, and what is written lands in `build/`, which
	the lint skips. So every emitted line in the four backends was governed by
	the emitters' own string literals -- which `literal_lines` deliberately
	excludes, that being where section 17's golden diagnostics live.

	One rule, two callers: `check_file` for the sources, and the test beside
	`every_schema.py` for what those sources produce.
	"""
	problems: list[Problem] = []

	for number, line in enumerate(text.splitlines(), start=1):
		if line != line.rstrip():
			problems.append(Problem(where, number, len(line.rstrip()) + 1,
			                        "trailing whitespace"))

		if "\r" in line:
			problems.append(Problem(where, number, line.index("\r") + 1, "carriage return"))

		if not indent or number in skip or is_comment_continuation(line):
			continue

		lead = leading_whitespace(line)
		# Tabs carry the indent level and spaces carry alignment within it, so a
		# space may follow a tab but never precede one.
		if " " in lead and "\t" in lead[lead.index(" ") :]:
			problems.append(Problem(where, number, lead.index(" ") + 1,
			                        "space before tab in indent"))
		elif lead.startswith(" ") and line.strip():
			problems.append(Problem(where, number, 1, "space-indented line; use tabs"))

	return problems


def check_file(path: Path, root: Path) -> list[Problem]:
	rel                      = path.relative_to(root)
	problems: list[Problem]  = []
	raw                      = path.read_bytes()

	try:
		text = raw.decode("ascii")
	except UnicodeDecodeError as exc:
		offset  = exc.start
		line_no = raw.count(b"\n", 0, offset) + 1
		col_no  = offset - (raw.rfind(b"\n", 0, offset) + 1) + 1
		problems.append(Problem(rel, line_no, col_no, f"non-ASCII byte 0x{raw[offset]:02x}"))
		return problems

	if raw and not raw.endswith(b"\n"):
		problems.append(Problem(rel, raw.count(b"\n") + 1, 1, "no newline at end of file"))

	problems.extend(check_text(text, rel, wants_indent_check(path), literal_lines(path)))
	return problems


def iter_sources(root: Path) -> list[Path]:
	found = []
	for path in sorted(root.rglob("*")):
		parts = path.relative_to(root).parts
		if any(part in SKIP_DIRS or part.startswith(".") for part in parts):
			continue
		if path.is_file() and wants_text_check(path):
			found.append(path)
	return found


def main(argv: list[str]) -> int:
	root     = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent
	problems = []

	for path in iter_sources(root):
		problems.extend(check_file(path, root))

	for problem in problems:
		print(problem, file=sys.stderr)

	if problems:
		print(f"\n{len(problems)} convention violation(s)", file=sys.stderr)
		return 1

	return 0


if __name__ == "__main__":
	raise SystemExit(main(sys.argv))
