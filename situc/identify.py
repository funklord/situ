"""How a file is recognised as a format: `identify` (26.1124).

An `identify` names evidence over a struct laid at the start of the file.
This compiles each item to TESTS at fixed offsets -- a run of bytes, one of
a set, a number in a range -- so that one read of the file's first `head`
bytes answers every format at once, and judges a file by the item roles:

- a `required` item that does not hold: no, whatever else held;
- otherwise a `sufficient` item that holds: certain;
- otherwise, where there is a `required` item and every one held: likely
  if a `supporting` item holds too, and possible if none does;
- otherwise no. A `supporting` item never stands alone: a `.png` whose
  bytes are not PNG's is not PNG, whatever its name says. A format with
  nothing better than its name says so by making the extension
  `sufficient` or `required` -- a hint, or a proof, by its role.

Within a level, formats rank by the bits the held items pinned -- a
tiebreak inside a named level, never a score across them, which is the
holder's rule: "numeric classification rarely works for cooperative
ventures".

THE FIRST TIER ONLY. An item at no fixed offset -- past a variable-length
run, behind a bit position, in a container member -- is refused naming
why, so the cheap tier cannot quietly grow a walk. The later tiers are
built when a format needs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from situc import ast
from situc.diagnostics import SituError, error
from situc.layout import Placement
from situc.resolve import ResolvedSchema

#: The verdicts, weakest first, so `max` picks the strongest.
LEVELS = ("no", "possible", "likely", "certain")

#: A comparison, as the inclusive range it admits of an unsigned field.
COMPARISONS = ("==", "!=", "<", "<=", ">", ">=")

#: What a text heuristic reads: the first this many bytes, or the file.
TEXT_WINDOW = 4096

#: The tiebreak inside a level, the more specific heuristic pinning more,
#: so a JSON file that is also valid UTF-8 is called JSON.
TEXT_BITS = {"utf8": 1, "ascii": 2, "csv": 4, "json": 8}


def _utf8(data: bytes, full: bool) -> bool:
	"""Strict UTF-8 with no control byte but tab, LF, FF and CR. A
	sequence cut by the window's end passes only when the window is full,
	since a shorter file really ended there. Written to be read beside
	`situ_id_utf8` in the C matcher, which is the same walk."""
	if not data:
		return False
	at = 0
	while at < len(data):
		byte = data[at]
		if byte < 0x80:
			if (byte < 0x20 and byte not in (9, 10, 12, 13)) or byte == 0x7f:
				return False
			at += 1
			continue
		if 0xc2 <= byte <= 0xdf:
			need, low, high = 1, 0x80, 0xbf
		elif byte == 0xe0:
			need, low, high = 2, 0xa0, 0xbf
		elif byte == 0xed:
			need, low, high = 2, 0x80, 0x9f
		elif 0xe1 <= byte <= 0xef:
			need, low, high = 2, 0x80, 0xbf
		elif byte == 0xf0:
			need, low, high = 3, 0x90, 0xbf
		elif 0xf1 <= byte <= 0xf3:
			need, low, high = 3, 0x80, 0xbf
		elif byte == 0xf4:
			need, low, high = 3, 0x80, 0x8f
		else:
			return False
		for step in range(1, need + 1):
			if at + step >= len(data):
				return full
			follow = data[at + step]
			if step == 1 and not low <= follow <= high:
				return False
			if step > 1 and not 0x80 <= follow <= 0xbf:
				return False
		at += need + 1
	return True


def _text(name: str, head: bytes) -> bool:
	data = head[:TEXT_WINDOW]
	if not _utf8(data, len(data) == TEXT_WINDOW):
		return False
	if name == "ascii":
		return all(byte < 0x80 for byte in data)
	if name == "json":
		at = 3 if data.startswith(b"\xef\xbb\xbf") else 0
		while at < len(data) and data[at] in b" \t\n\r":
			at += 1
		return at < len(data) and data[at] in b"{["
	if name == "csv":
		counts, commas, quoted = [], 0, False
		for byte in data:
			if byte == 0x22:
				quoted = not quoted
			elif byte == 0x2c and not quoted:
				commas += 1
			elif byte == 0x0a and not quoted:
				counts.append(commas)
				commas = 0
		return (len(counts) >= 2 and counts[0] >= 1
		        and all(count == counts[0] for count in counts))
	return True


@dataclass(frozen=True)
class Test:
	"""One check at a fixed byte offset. `runs` for bytes that must be one
	of a set (a preamble is a set of one); `low`/`high` for a number;
	`not_equal` for `!=`."""

	offset: int
	width: int
	runs: tuple[bytes, ...] = ()
	low: int | None = None
	high: int | None = None
	not_equal: int | None = None
	big: bool = True
	#: A named text heuristic over the first TEXT_WINDOW bytes, in place
	#: of everything above.
	text: str | None = None

	def holds(self, head: bytes) -> bool:
		if self.text is not None:
			return _text(self.text, head)
		if len(head) < self.offset + self.width:
			return False
		here = head[self.offset:self.offset + self.width]
		if self.runs:
			return here in self.runs
		value = int.from_bytes(here, "big" if self.big else "little")
		if self.not_equal is not None and value == self.not_equal:
			return False
		if self.low is not None and value < self.low:
			return False
		return self.high is None or value <= self.high

	@property
	def pinned_bits(self) -> int:
		"""What holding it says, for the tiebreak: every bit of a single
		run, fewer for a set or a range."""
		if self.text is not None:
			return TEXT_BITS[self.text]
		exact = len(self.runs) == 1 or (self.low is not None
		                                and self.low == self.high)
		if exact:
			return 8 * self.width
		return 4 * self.width if self.runs else 1


@dataclass(frozen=True)
class Item:
	role: str
	tests: tuple[Test, ...] = ()
	extensions: tuple[str, ...] = ()

	def holds(self, head: bytes, name: str) -> bool:
		if self.extensions:
			lowered = _ascii_lower(name)
			return any(lowered.endswith("." + _ascii_lower(one))
			           for one in self.extensions)
		return all(test.holds(head) for test in self.tests)


@dataclass(frozen=True)
class Recogniser:
	name: str
	media: str | None
	items: tuple[Item, ...]
	#: The struct the file starts with, which a reader of it opens; None
	#: for a text format.
	struct: str | None = None

	@property
	def head(self) -> int:
		"""The bytes of the file this format needs to be judged."""
		return max((test.offset + test.width for item in self.items
		            for test in item.tests), default=0)

	def judge(self, head: bytes, name: str) -> tuple[str, int]:
		held = [item for item in self.items if item.holds(head, name)]
		bits = sum(test.pinned_bits for item in held for test in item.tests)
		required = [item for item in self.items if item.role == "required"]
		if any(item not in held for item in required):
			return "no", 0
		roles = {item.role for item in held}
		if "sufficient" in roles:
			return "certain", bits
		if not required:
			return "no", 0
		return ("likely" if "supporting" in roles else "possible"), bits


def _ascii_lower(text: str) -> str:
	"""Case folded as the generated C folds it, A-Z only: a name is
	compared byte for byte, and a matcher that folded more in one
	language than the other would disagree about the same file."""
	return "".join(chr(ord(one) + 32) if "A" <= one <= "Z" else one
	               for one in text)


def _path(expr: ast.Expr) -> str | None:
	if isinstance(expr, ast.NameRef):
		return expr.name
	if isinstance(expr, ast.Access):
		base = _path(expr.base)
		return None if base is None else f"{base}.{expr.name}"
	return None


class _Compiler:
	def __init__(self, decl: ast.Identify, resolved: ResolvedSchema,
			enums: dict[str, ast.EnumDecl]) -> None:
		self.decl     = decl
		self.resolved = resolved
		self.enums    = enums
		self.entries: dict[str, Placement] = {}
		if decl.param is None:
			return
		struct = resolved.structs.get(decl.param.type_name)
		if struct is None:
			raise error(f"`{decl.name}` reads `{decl.param.type_name}`, "
			            "which is not a struct", decl.param.span,
			            label="here")
		self.entries = {entry.placement.path: entry.placement
		                for entry in struct.entries}

	@property
	def param(self) -> ast.RelationParam:
		assert self.decl.param is not None
		return self.decl.param

	def refuse(self, why: str, where: ast.Node) -> SituError:
		return error(f"`{self.decl.name}`: {why}", where.span, label="here",
		             notes=["the first tier reads fixed offsets from the "
		                    "start of the file only, so one read answers "
		                    "every format (26.1124)"])

	def placement(self, path: str, where: ast.Node) -> Placement:
		if self.decl.param is None:
			raise self.refuse(f"`{path}` names a field, and a format with no "
			                  "struct at its start has none: its evidence "
			                  "is `text` or `extension`", where)
		root, _, rest = path.partition(".")
		if root != self.param.name:
			raise self.refuse(f"`{path}` names no field of "
			                  f"`{self.param.name}`", where)
		flat = self.param.type_name + (f".{rest}" if rest else "")
		found = self.entries.get(flat)
		if found is None:
			raise self.refuse(f"`{path}` names no field of "
			                  f"`{self.param.type_name}`", where)
		return found

	def fixed(self, place: Placement, path: str, where: ast.Node
			) -> tuple[int, int]:
		if place.offset_bits is None or place.frame_relative:
			raise self.refuse(f"`{path}` is at no fixed offset: something "
			                  "of variable length comes before it", where)
		if place.offset_bits % 8 or place.size_bits % 8:
			raise self.refuse(f"`{path}` is not whole bytes", where)
		if place.size_max_bits != place.size_bits:
			raise self.refuse(f"`{path}` is of no fixed size", where)
		return place.offset_bits // 8, place.size_bits // 8

	def _int(self, expr: ast.Expr | None, where: ast.Node) -> int:
		if isinstance(expr, ast.IntLiteral):
			return expr.value
		raise self.refuse("a bound must be a number here", where)

	def own(self, place: Placement, path: str, where: ast.Node
			) -> list[Test]:
		"""The member's own constraint, as tests: what the format already
		says, so the evidence need not say it again."""
		tests: list[Test] = []
		if place.scalar is None and place.pinned_runs is None:
			return tests
		offset, width = self.fixed(place, path, where)
		big = place.endian is not ast.Endian.LITTLE
		if place.pinned_runs is not None:
			tests.append(Test(offset, width, runs=place.pinned_runs))
		enum = self.enums.get(place.type_name)
		if (enum is not None and place.pinned_runs is None
				and enum.effective_default is ast.EnumDefault.ERROR):
			values = self.resolved.layout.env.enums.get(enum.name, {})
			tests.append(Test(offset, width, runs=tuple(
				value.to_bytes(width, "big" if big else "little")
				for value in values.values())))
		said = {attr.name: attr for attr in place.attrs}
		if "must_eq" in said:
			value = said["must_eq"].value
			if isinstance(value, ast.StringLiteral):
				tests.append(Test(offset, width,
				                  runs=(value.value.encode("latin-1"),)))
			else:
				number = self._int(value, where)
				tests.append(Test(offset, width, low=number, high=number,
				                  big=big))
		if ("min" in said or "max" in said) and width <= 8:
			tests.append(Test(
				offset, width, big=big,
				low=self._int(said["min"].value, where) if "min" in said
				else None,
				high=self._int(said["max"].value, where) if "max" in said
				else None))
		return tests

	def bare(self, path: str, where: ast.Node) -> list[Test]:
		"""A member's own constraints and those of everything inside it;
		the parameter alone is every constraint the struct states."""
		if self.decl.param is not None and path == self.param.name:
			tests, prefix = [], self.param.type_name + "."
		else:
			place = self.placement(path, where)
			tests, prefix = self.own(place, path, where), place.path + "."
		for inner, nested in self.entries.items():
			if inner.startswith(prefix):
				tests += self.own(nested, inner, where)
		if not tests:
			raise self.refuse(f"`{path}` states nothing to check: no "
			                  "preamble, `must_eq`, closed enum or bound",
			                  where)
		return tests

	def compared(self, expr: ast.Binary, where: ast.Node) -> list[Test]:
		path = _path(expr.left)
		if path is None:
			raise self.refuse("a comparison reads a field on its left",
			                  where)
		place = self.placement(path, where)
		offset, width = self.fixed(place, path, where)
		big = place.endian is not ast.Endian.LITTLE
		right = expr.right
		if isinstance(right, ast.NameRef):
			byte_arms = self.resolved.layout.env.byte_enums.get(
				place.type_name, {})
			arms = self.resolved.layout.env.enums.get(place.type_name, {})
			if right.name in byte_arms and expr.op == "==":
				return [Test(offset, width, runs=(byte_arms[right.name],))]
			if right.name not in arms:
				raise self.refuse(f"`{right.name}` is not a member of "
				                  f"`{place.type_name}`", where)
			right = ast.IntLiteral(right.span, arms[right.name],
			                       right.name)
		if isinstance(right, ast.StringLiteral):
			if expr.op != "==":
				raise self.refuse("bytes compare only with `==`", where)
			run = right.value.encode("latin-1")
			if len(run) != width:
				raise self.refuse(f"`{path}` is {width} bytes and the "
				                  f"literal {len(run)}", where)
			return [Test(offset, width, runs=(run,))]
		value = self._int(right, where)
		if width > 8:
			raise self.refuse(f"`{path}` is {width} bytes, wider than a "
			                  "number this tier compares", where)
		if value < 0 or (value == 0 and expr.op == "<"):
			raise self.refuse(f"`{path}` is unsigned here, and "
			                  f"`{expr.op} {value}` could never hold", where)
		if expr.op == "!=":
			return [Test(offset, width, not_equal=value, big=big)]
		low, high = {
			"==": (value, value), "<": (None, value - 1),
			"<=": (None, value), ">": (value + 1, None),
			">=": (value, None)}[expr.op]
		return [Test(offset, width, low=low, high=high, big=big)]

	def tests(self, expr: ast.Expr, where: ast.Node) -> list[Test]:
		if isinstance(expr, ast.Binary) and expr.op == "&&":
			return self.tests(expr.left, where) + self.tests(expr.right,
			                                                 where)
		if isinstance(expr, ast.Binary) and expr.op in COMPARISONS:
			return self.compared(expr, where)
		path = _path(expr)
		if path is None:
			raise self.refuse("evidence is a path, a comparison, or "
			                  "`extension`", where)
		return self.bare(path, where)

	def compile(self) -> Recogniser:
		items = []
		for line in self.decl.evidence:
			if line.text is not None:
				items.append(Item(line.role, (Test(0, TEXT_WINDOW,
				                                   text=line.text),)))
			elif line.extensions:
				items.append(Item(line.role, extensions=line.extensions))
			else:
				assert line.expr is not None
				items.append(Item(line.role,
				                  tuple(self.tests(line.expr, line))))
		if not any(item.role in ("sufficient", "required")
		           for item in items):
			raise error(f"`{self.decl.name}` has nothing `sufficient` or "
			            "`required`, so no file could be more than nothing",
			            self.decl.span, label="here")
		said = {attr.name: attr for attr in self.decl.attrs}
		media = said["media"].value if "media" in said else None
		return Recogniser(
			self.decl.name,
			media.value if isinstance(media, ast.StringLiteral) else None,
			tuple(items),
			None if self.decl.param is None else self.param.type_name)


def recognisers(schema: ast.Schema, resolved: ResolvedSchema
		) -> list[Recogniser]:
	"""Every `identify` in the schema, compiled; a SituError names the
	first item the first tier cannot read."""
	enums = {decl.name: decl for decl in schema.decls
	         if isinstance(decl, ast.EnumDecl)}
	file = any(isinstance(decl, ast.TargetDirective)
	           and decl.kind is ast.TargetKind.FILE for decl in schema.decls)
	for decl in schema.identifies():
		if not file:
			raise error(f"`{decl.name}` recognises a file, and this schema "
			            "is not `target file`", decl.span, label="here",
			            notes=["a file format is more than a buffer: say "
			                   "`target file;` (26.175, 26.1124)"])
	return [_Compiler(decl, resolved, enums).compile()
	        for decl in schema.identifies()]


def best(formats: list[Recogniser], head: bytes, name: str
		) -> tuple[Recogniser | None, str]:
	"""The strongest verdict, the most bits pinned breaking a tie."""
	ranked = [(LEVELS.index(level), bits, -at, one, level)
	          for at, one in enumerate(formats)
	          for level, bits in [one.judge(head, name)] if level != "no"]
	if not ranked:
		return None, "no"
	*_, chosen, level = max(ranked)
	return chosen, level


def identify_files(formats: list[Recogniser], files: list[Path]
		) -> list[tuple[Path, Recogniser | None, str]]:
	need = max((one.head for one in formats), default=0)
	found = []
	for path in files:
		with path.open("rb") as stream:
			head = stream.read(need)
		chosen, level = best(formats, head, path.name)
		found.append((path, chosen, level))
	return found


def _magic_conditions(test: Test) -> list[str]:
	"""`offset<TAB>type<TAB>test` for each condition the test is, or none
	where magic(5) cannot say it."""
	if len(test.runs) == 1:
		escaped = "".join(chr(byte) if 0x21 <= byte < 0x7f and chr(byte)
		                  not in "\\ " else f"\\x{byte:02x}"
		                  for byte in test.runs[0])
		return [f"{test.offset}\tstring\t{escaped}"]
	kinds = {1: "byte", 2: "short", 4: "long", 8: "quad"}
	if test.runs or test.width not in kinds:
		return []
	kind = "ubyte" if test.width == 1 else \
		("be" if test.big else "le") + kinds[test.width]
	at = f"{test.offset}\t{kind}"
	if test.not_equal is not None:
		return [f"{at}\t!{test.not_equal}"]
	if test.low is not None and test.low == test.high:
		return [f"{at}\t{test.low}"]
	said = []
	if test.low is not None:
		said.append(f"{at}\t>{test.low - 1}")
	if test.high is not None:
		said.append(f"{at}\t<{test.high + 1}")
	return said


def magic(formats: list[Recogniser]) -> str:
	"""The `certain` verdicts magic(5) can say: each sufficient item's
	tests, then every required item's, as continuation lines. `likely` and
	`possible` have no magic(5) spelling, and a set of byte runs none
	either; each left out is named in a comment."""
	lines = ["# Generated by situc from `identify` declarations (26.1124)."]
	for one in formats:
		required = [test for item in one.items if item.role == "required"
		            for test in item.tests]
		extensions = [ext for item in one.items for ext in item.extensions]
		for item in one.items:
			if item.role != "sufficient" or not item.tests:
				continue
			said = [_magic_conditions(test)
			        for test in [*item.tests, *required]]
			if not all(said):
				lines.append(f"# {one.name}: an item with no magic(5) "
				             "spelling -- a set of runs, a text heuristic -- "
				             "is left out")
				continue
			# Each condition one level deeper, and the name on the last:
			# magic(5) prints a line's message when that line matches, so
			# a name on the first would be printed with the rest unchecked.
			flat = [condition for group in said for condition in group]
			for depth, condition in enumerate(flat):
				end = f"\t{one.name}" if depth == len(flat) - 1 else ""
				lines.append(f"{'>' * depth}{condition}{end}")
			if one.media:
				lines.append(f"!:mime\t{one.media}")
			if extensions:
				lines.append(f"!:ext\t{'/'.join(extensions)}")
		lines.append("")
	return "\n".join(lines)


def _c_string(text: str) -> str:
	return '"' + "".join(one if 0x20 <= ord(one) < 0x7f and one not in '"\\?'
	                     else f"\\{ord(one):03o}" for one in text) + '"'


def _c_bytes(data: bytes) -> str:
	return "{" + ", ".join(f"0x{byte:02x}" for byte in data) + "}"


#: The C matcher's number for each heuristic, 0 being none.
_TEXT_CODES = {"utf8": 1, "ascii": 2, "csv": 3, "json": 4}


def c_source(formats: list[Recogniser], prefix: str = "situ_id") -> str:
	"""One C file: every format's tests as tables, and `<prefix>_judge`,
	which answers them all from one read of a file's first
	`<PREFIX>_HEAD` bytes -- the matcher a multi-file search links
	(26.1124). The verdict and its tiebreak are `best`'s, and the suite
	holds the two to each other on the same files."""
	upper = prefix.upper()
	roles = {"sufficient": 0, "required": 1, "supporting": 2}
	lines = [
		"/* Generated by situc identify --c (26.1124) -- do not edit. */",
		"#include <stddef.h>",
		"#include <stdint.h>",
		"#include <string.h>",
		"",
		f"enum {{ {upper}_NO, {upper}_POSSIBLE, {upper}_LIKELY, "
		f"{upper}_CERTAIN }};",
		f"#define {upper}_HEAD "
		f"{max((one.head for one in formats), default=0)}u",
		"",
		"struct " + prefix + "_test {",
		"\tuint32_t offset, width, runs;\t/* runs: how many in `run` */",
		"\tconst uint8_t *run;",
		"\tuint8_t has_low, has_high, has_not, big;",
		"\tuint8_t text;\t/* 0 none; 1 utf8, 2 ascii, 3 csv, 4 json */",
		"\tuint64_t low, high, not_equal;",
		"\tuint32_t bits;",
		"};",
		"struct " + prefix + "_item {",
		"\tuint8_t role;\t/* 0 sufficient, 1 required, 2 supporting */",
		f"\tconst struct {prefix}_test *tests;",
		"\tuint32_t count;",
		"\tconst char *const *extensions;",
		"\tuint32_t extension_count;",
		"};",
		"struct " + prefix + "_format {",
		"\tconst char *name, *media;",
		f"\tconst struct {prefix}_item *items;",
		"\tuint32_t count;",
		"};",
		"",
	]
	format_rows = []
	for at, one in enumerate(formats):
		item_rows = []
		for index, item in enumerate(one.items):
			stem = f"{prefix}_{at}_{index}"
			test_rows = []
			for number, test in enumerate(item.tests):
				run = "NULL"
				if test.runs:
					lines.append(f"static const uint8_t {stem}_{number}[] = "
					             f"{_c_bytes(b''.join(test.runs))};")
					run = f"{stem}_{number}"
				test_rows.append(
					f"\t{{{test.offset}u, {test.width}u, {len(test.runs)}u, "
					f"{run}, {int(test.low is not None)}, "
					f"{int(test.high is not None)}, "
					f"{int(test.not_equal is not None)}, {int(test.big)}, "
					f"{_TEXT_CODES.get(test.text or '', 0)}, "
					f"{test.low or 0}u, {test.high or 0}u, "
					f"{test.not_equal or 0}u, {test.pinned_bits}u}},")
			tests = "NULL"
			if test_rows:
				lines += [f"static const struct {prefix}_test {stem}_tests[] "
				          "= {", *test_rows, "};"]
				tests = f"{stem}_tests"
			extensions = "NULL"
			if item.extensions:
				lines.append(f"static const char *const {stem}_extensions[] "
				             "= {" + ", ".join(_c_string(_ascii_lower(ext))
				                               for ext in item.extensions)
				             + "};")
				extensions = f"{stem}_extensions"
			item_rows.append(f"\t{{{roles[item.role]}, {tests}, "
			                 f"{len(item.tests)}u, {extensions}, "
			                 f"{len(item.extensions)}u}},")
		lines += [f"static const struct {prefix}_item {prefix}_{at}_items[] "
		          "= {", *item_rows, "};"]
		media = _c_string(one.media) if one.media else "NULL"
		format_rows.append(f"\t{{{_c_string(one.name)}, {media}, "
		                   f"{prefix}_{at}_items, {len(one.items)}u}},")
	lines += [
		f"static const struct {prefix}_format {prefix}_formats[] = {{",
		*(format_rows or ["\t{NULL, NULL, NULL, 0u},"]),
		"};",
		f"static const uint32_t {prefix}_format_count = {len(formats)}u;",
		"",
		"/* Strict UTF-8 with no control byte but tab, LF, FF and CR; a",
		" * sequence cut by the window's end passes only when the window is",
		" * full. The same walk as `_utf8` in situc/identify.py. */",
		f"static int {prefix}_utf8(const uint8_t *data, size_t length, "
		"int full)",
		"{",
		"\tsize_t at = 0;",
		"",
		"\tif (length == 0u) {",
		"\t\treturn 0;",
		"\t}",
		"\twhile (at < length) {",
		"\t\tconst uint8_t byte = data[at];",
		"\t\tunsigned need, low = 0x80u, high = 0xbfu;",
		"",
		"\t\tif (byte < 0x80u) {",
		"\t\t\tif ((byte < 0x20u && byte != 9u && byte != 10u && byte != 12u",
		"\t\t\t     && byte != 13u) || byte == 0x7fu) {",
		"\t\t\t\treturn 0;",
		"\t\t\t}",
		"\t\t\tat++;",
		"\t\t\tcontinue;",
		"\t\t}",
		"\t\tif (byte >= 0xc2u && byte <= 0xdfu) {",
		"\t\t\tneed = 1u;",
		"\t\t} else if (byte == 0xe0u) {",
		"\t\t\tneed = 2u; low = 0xa0u;",
		"\t\t} else if (byte == 0xedu) {",
		"\t\t\tneed = 2u; high = 0x9fu;",
		"\t\t} else if (byte >= 0xe1u && byte <= 0xefu) {",
		"\t\t\tneed = 2u;",
		"\t\t} else if (byte == 0xf0u) {",
		"\t\t\tneed = 3u; low = 0x90u;",
		"\t\t} else if (byte >= 0xf1u && byte <= 0xf3u) {",
		"\t\t\tneed = 3u;",
		"\t\t} else if (byte == 0xf4u) {",
		"\t\t\tneed = 3u; high = 0x8fu;",
		"\t\t} else {",
		"\t\t\treturn 0;",
		"\t\t}",
		"\t\tfor (unsigned step = 1u; step <= need; step++) {",
		"\t\t\tuint8_t follow;",
		"",
		"\t\t\tif (at + step >= length) {",
		"\t\t\t\treturn full;",
		"\t\t\t}",
		"\t\t\tfollow = data[at + step];",
		"\t\t\tif (step == 1u ? (follow < low || follow > high)",
		"\t\t\t                : (follow < 0x80u || follow > 0xbfu)) {",
		"\t\t\t\treturn 0;",
		"\t\t\t}",
		"\t\t}",
		"\t\tat += need + 1u;",
		"\t}",
		"\treturn 1;",
		"}",
		"",
		f"static int {prefix}_text(unsigned which, const uint8_t *head, "
		"size_t length)",
		"{",
		f"\tconst size_t window = length < {TEXT_WINDOW}u ? length : "
		f"{TEXT_WINDOW}u;",
		"\tsize_t at = 0, lines = 0, first = 0, commas = 0;",
		"\tint quoted = 0;",
		"",
		f"\tif (!{prefix}_utf8(head, window, window == {TEXT_WINDOW}u)) {{",
		"\t\treturn 0;",
		"\t}",
		"\tswitch (which) {",
		"\tcase 2u:",
		"\t\tfor (at = 0; at < window; at++) {",
		"\t\t\tif (head[at] >= 0x80u) {",
		"\t\t\t\treturn 0;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn 1;",
		"\tcase 4u:",
		"\t\tif (window >= 3u && head[0] == 0xefu && head[1] == 0xbbu",
		"\t\t    && head[2] == 0xbfu) {",
		"\t\t\tat = 3u;",
		"\t\t}",
		"\t\twhile (at < window && (head[at] == ' ' || head[at] == '\\t'",
		"\t\t       || head[at] == '\\n' || head[at] == '\\r')) {",
		"\t\t\tat++;",
		"\t\t}",
		"\t\treturn at < window && (head[at] == '{' || head[at] == '[');",
		"\tcase 3u:",
		"\t\tfor (at = 0; at < window; at++) {",
		"\t\t\tif (head[at] == '\"') {",
		"\t\t\t\tquoted = !quoted;",
		"\t\t\t} else if (head[at] == ',' && !quoted) {",
		"\t\t\t\tcommas++;",
		"\t\t\t} else if (head[at] == '\\n' && !quoted) {",
		"\t\t\t\tif (lines == 0u) {",
		"\t\t\t\t\tfirst = commas;",
		"\t\t\t\t} else if (commas != first) {",
		"\t\t\t\t\treturn 0;",
		"\t\t\t\t}",
		"\t\t\t\tlines++;",
		"\t\t\t\tcommas = 0;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn lines >= 2u && first >= 1u;",
		"\tdefault:",
		"\t\treturn 1;",
		"\t}",
		"}",
		"",
		f"static int {prefix}_test_holds(const struct {prefix}_test *test,",
		"\t\tconst uint8_t *head, size_t length)",
		"{",
		"\tuint64_t value = 0;",
		"",
		"\tif (test->text != 0u) {",
		f"\t\treturn {prefix}_text(test->text, head, length);",
		"\t}",
		"\tif (length < (size_t)test->offset + test->width) {",
		"\t\treturn 0;",
		"\t}",
		"\tif (test->runs != 0u) {",
		"\t\tfor (uint32_t at = 0; at < test->runs; at++) {",
		"\t\t\tif (memcmp(head + test->offset, test->run + "
		"(size_t)at * test->width, test->width) == 0) {",
		"\t\t\t\treturn 1;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn 0;",
		"\t}",
		"\tfor (uint32_t at = 0; at < test->width; at++) {",
		"\t\tconst uint32_t from = test->big ? at : test->width - 1u - at;",
		"\t\tvalue = (value << 8) | head[test->offset + from];",
		"\t}",
		"\tif (test->has_not && value == test->not_equal) {",
		"\t\treturn 0;",
		"\t}",
		"\tif (test->has_low && value < test->low) {",
		"\t\treturn 0;",
		"\t}",
		"\treturn !test->has_high || value <= test->high;",
		"}",
		"",
		f"static int {prefix}_named(const char *name, const char *extension)",
		"{",
		"\tconst size_t whole = strlen(name), tail = strlen(extension);",
		"",
		"\tif (whole < tail + 1u || name[whole - tail - 1u] != '.') {",
		"\t\treturn 0;",
		"\t}",
		"\tfor (size_t at = 0; at < tail; at++) {",
		"\t\tchar one = name[whole - tail + at];",
		"\t\tif (one >= 'A' && one <= 'Z') {",
		"\t\t\tone = (char)(one + 32);",
		"\t\t}",
		"\t\tif (one != extension[at]) {",
		"\t\t\treturn 0;",
		"\t\t}",
		"\t}",
		"\treturn 1;",
		"}",
		"",
		"/* The strongest verdict over every format, the most bits pinned",
		" * breaking a tie and the earlier format a tie of those; `head` is",
		f" * the file's first {upper}_HEAD bytes, or all of it if shorter. */",
		f"static int {prefix}_judge(const uint8_t *head, size_t length,",
		f"\t\tconst char *name, const struct {prefix}_format **chosen)",
		"{",
		f"\tint best = {upper}_NO;",
		"\tuint64_t best_bits = 0;",
		"",
		"\t*chosen = NULL;",
		f"\tfor (uint32_t at = 0; at < {prefix}_format_count; at++) {{",
		f"\t\tconst struct {prefix}_format *format = &{prefix}_formats[at];",
		"\t\tint sufficient = 0, supporting = 0, required = 0, failed = 0;",
		"\t\tuint64_t bits = 0;",
		"\t\tint level;",
		"",
		"\t\tfor (uint32_t index = 0; index < format->count; index++) {",
		f"\t\t\tconst struct {prefix}_item *item = &format->items[index];",
		"\t\t\tint holds = 1;",
		"\t\t\tuint64_t pinned = 0;",
		"",
		"\t\t\tif (item->extension_count != 0u) {",
		"\t\t\t\tholds = 0;",
		"\t\t\t\tfor (uint32_t one = 0; one < item->extension_count; "
		"one++) {",
		f"\t\t\t\t\tholds |= {prefix}_named(name, "
		"item->extensions[one]);",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t\tfor (uint32_t one = 0; holds && one < item->count; one++) {",
		f"\t\t\t\tholds = {prefix}_test_holds(&item->tests[one], head, "
		"length);",
		"\t\t\t\tpinned += item->tests[one].bits;",
		"\t\t\t}",
		"\t\t\tif (item->role == 1u) {",
		"\t\t\t\trequired = 1;",
		"\t\t\t\tfailed |= !holds;",
		"\t\t\t}",
		"\t\t\tif (!holds) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tbits += pinned;",
		"\t\t\tsufficient |= item->role == 0u;",
		"\t\t\tsupporting |= item->role == 2u;",
		"\t\t}",
		"\t\tif (failed) {",
		f"\t\t\tlevel = {upper}_NO;",
		"\t\t} else if (sufficient) {",
		f"\t\t\tlevel = {upper}_CERTAIN;",
		"\t\t} else if (!required) {",
		f"\t\t\tlevel = {upper}_NO;",
		"\t\t} else {",
		f"\t\t\tlevel = supporting ? {upper}_LIKELY : {upper}_POSSIBLE;",
		"\t\t}",
		f"\t\tif (level != {upper}_NO && (level > best",
		"\t\t\t\t|| (level == best && bits > best_bits))) {",
		"\t\t\tbest = level;",
		"\t\t\tbest_bits = bits;",
		"\t\t\t*chosen = format;",
		"\t\t}",
		"\t}",
		"\treturn best;",
		"}",
		"",
	]
	return "\n".join(lines)


def _mime_match(test: Test) -> tuple[str, str] | None:
	"""A shared-mime-info `<match>`'s type and value, or None where it has
	no spelling: a set of runs, a range, `!=`."""
	if len(test.runs) == 1:
		value = "".join(
			chr(byte) if 0x20 <= byte < 0x7f and chr(byte) != "\\"
			else "\\\\" if chr(byte) == "\\" else f"\\x{byte:02x}"
			for byte in test.runs[0])
		return "string", value
	kinds = {1: "byte", 2: "16", 4: "32"}
	exact = test.low is not None and test.low == test.high
	if test.runs or not exact or test.width not in kinds:
		return None
	kind = kinds[test.width] if test.width == 1 else \
		("big" if test.big else "little") + kinds[test.width]
	return kind, str(test.low)


def mime_info(formats: list[Recogniser]) -> str:
	"""A shared-mime-info package: each format's media type, its
	extensions as globs, and its `certain` verdicts as nested matches --
	the same rules magic(5) gets, for the desktop's own detector. A
	format with no `media` has no type to declare, and a test with no
	`<match>` spelling leaves its item out; each is said in a comment."""
	from xml.sax.saxutils import escape, quoteattr
	lines = ['<?xml version="1.0" encoding="UTF-8"?>',
	         "<!-- Generated by situc identify, as a shared-mime-info package "
	         "(26.1124). -->",
	         '<mime-info xmlns="http://www.freedesktop.org/standards/'
	         'shared-mime-info">']
	for one in formats:
		if one.media is None:
			lines.append(f"  <!-- {escape(one.name)}: no `media`, so no "
			             "type to declare -->")
			continue
		lines += [f"  <mime-type type={quoteattr(one.media)}>",
		          f"    <comment>{escape(one.name)}</comment>"]
		required = [test for item in one.items if item.role == "required"
		            for test in item.tests]
		for item in one.items:
			if item.role != "sufficient" or not item.tests:
				continue
			said = [_mime_match(test) for test in [*item.tests, *required]]
			if not all(said):
				lines.append("    <!-- a sufficient item with no <match> "
				             "spelling is left out -->")
				continue
			lines.append('    <magic priority="80">')
			tests = [*item.tests, *required]
			for depth, (test, match) in enumerate(zip(tests, said)):
				assert match is not None
				kind, value = match
				close = "/>" if depth == len(tests) - 1 else ">"
				lines.append(f"{'  ' * (depth + 3)}<match type="
				             f"{quoteattr(kind)} value={quoteattr(value)} "
				             f'offset="{test.offset}"{close}')
			for depth in reversed(range(len(tests) - 1)):
				lines.append(f"{'  ' * (depth + 3)}</match>")
			lines.append("    </magic>")
		for item in one.items:
			for extension in item.extensions:
				weight = ' weight="80"' if item.role == "sufficient" else ""
				lines.append(f"    <glob pattern={quoteattr('*.' + extension)}"
				             f"{weight}/>")
		lines.append("  </mime-type>")
	lines += ["</mime-info>", ""]
	return "\n".join(lines)
