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

	def holds(self, head: bytes) -> bool:
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
			lowered = name.lower()
			return any(lowered.endswith("." + one.lower())
			           for one in self.extensions)
		return all(test.holds(head) for test in self.tests)


@dataclass(frozen=True)
class Recogniser:
	name: str
	media: str | None
	items: tuple[Item, ...]

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
		struct = resolved.structs.get(decl.param.type_name)
		if struct is None:
			raise error(f"`{decl.name}` reads `{decl.param.type_name}`, "
			            "which is not a struct", decl.param.span,
			            label="here")
		self.entries = {entry.placement.path: entry.placement
		                for entry in struct.entries}

	def refuse(self, why: str, where: ast.Node) -> SituError:
		return error(f"`{self.decl.name}`: {why}", where.span, label="here",
		             notes=["the first tier reads fixed offsets from the "
		                    "start of the file only, so one read answers "
		                    "every format (26.1124)"])

	def placement(self, path: str, where: ast.Node) -> Placement:
		root, _, rest = path.partition(".")
		if root != self.decl.param.name:
			raise self.refuse(f"`{path}` names no field of "
			                  f"`{self.decl.param.name}`", where)
		flat = self.decl.param.type_name + (f".{rest}" if rest else "")
		found = self.entries.get(flat)
		if found is None:
			raise self.refuse(f"`{path}` names no field of "
			                  f"`{self.decl.param.type_name}`", where)
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
		if "min" in said or "max" in said:
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
		if path == self.decl.param.name:
			tests, prefix = [], self.decl.param.type_name + "."
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
			if line.extensions:
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
			tuple(items))


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
				lines.append(f"# {one.name}: a set of byte runs has no "
				             "magic(5) spelling; left out")
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
