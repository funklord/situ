"""A registry of typed cells, in Python (0070, step 6).

The C's table and four functions (26.1081) in Python's idiom: a class per
registry holding its entries, and `find`, `check`, `value` and `safer` as
static methods. `check` raises the cell type's own error -- a cell that is
not one is refused exactly as `validate` refuses the message -- and a bound
the entry states raises `ConstraintError`.
"""

from __future__ import annotations

from collections.abc import Mapping

from situc import ast
from situc.wellformed import _policy_words, cell_value


def _attr(entry: ast.RegistryEntry, name: str) -> ast.Attr | None:
	return next((attr for attr in entry.attrs if attr.name == name), None)


def _literal(entry: ast.RegistryEntry, name: str) -> object:
	held = _attr(entry, name)
	if held is None:
		return None
	value = held.value
	if isinstance(value, (ast.IntLiteral, ast.StringLiteral)):
		return value.value
	return getattr(value, "name", None)


def policy_constants(schema: ast.Schema) -> list[str]:
	"""Each declared policy's words as bits, once per module."""
	if not schema.registries():
		return []
	lines = ["", "# `safe`: which of two values written apart stands (0070).",
	         "SAFE_LOWER = 1", "SAFE_HIGHER = 2"]
	for policy in schema.policies():
		lines.append("")
		lines.append(f"# policy {policy.name}")
		for bit, word in enumerate(policy.words):
			lines.append(f"{policy.name.upper()}_{word.upper()} = {1 << bit:#x}")
	return lines


def registry_class(schema: ast.Schema, decl: ast.RegistryDecl,
		structs: Mapping[str, object], py_name: object) -> list[str]:
	"""The class for one registry."""
	name = py_name(decl.name)				# type: ignore[operator]
	types = list(dict.fromkeys(entry.type_name for entry in decl.entries))
	policies = [policy.name for policy in schema.policies()]
	decls = {one.name: one for one in schema.structs()}
	lines = [
		"",
		"",
		f"class {name}:",
		f'\t"""registry {decl.name} (0070): a keyspace of typed cells.',
		"",
		"\tA key ending in '/' is a family, whose instances add one word of",
		"\ta-z and 0-9. Each cell's bytes are a message of its type, checked",
		'\tby that type\'s own `validate` and then by the entry\'s bounds."""',
		"",
		"\tclass Entry:",
		'\t\t"""One cell: its key, bounds and what a client shows."""',
		"",
		"\t\tdef __init__(self, key: str, family: bool, safe: int,",
		"\t\t             min: int | None, max: int | None,",
		"\t\t             min_size: int, max_size: int,",
		"\t\t             default: str | None, unit: str | None,",
		"\t\t             summary: str | None, window_ms: int, cell: int"
		+ "".join(f", {policy}: int" for policy in policies) + ") -> None:",
		*(f"\t\t\tself.{field} = {field}" for field in (
			"key", "family", "safe", "min", "max", "min_size", "max_size",
			"default", "unit", "summary", "window_ms", "cell",
			*policies)),
		"",
		"\t# Which type a cell is: an entry's `cell`, by name (26.1169).",
		*(f"\tCELL_{type_name.upper()} = {index}"
		  for index, type_name in enumerate(types)),
		"",
		"\tENTRIES: tuple[\"Entry\", ...] = ()",
		"",
		"\t@staticmethod",
		f"\tdef find(key: str) -> \"{name}.Entry | None\":",
		'\t\t"""The entry `key` is: exact, or a family\'s instance."""',
		f"\t\tfor entry in {name}.ENTRIES:",
		"\t\t\tif not entry.family:",
		"\t\t\t\tif key == entry.key:",
		"\t\t\t\t\treturn entry",
		"\t\t\t\tcontinue",
		"\t\t\tword = key[len(entry.key):]",
		"\t\t\tif key.startswith(entry.key) and word and all(",
		"\t\t\t\t\t\"a\" <= c <= \"z\" or \"0\" <= c <= \"9\" for c in word):",
		"\t\t\t\treturn entry",
		"\t\treturn None",
		"",
		"\t@staticmethod",
		"\tdef _cell(cell: int, data: bytes | bytearray | memoryview)"
		" -> int | None:",
		'\t\t"""Validate a cell of type `cell`; its value, or None."""',
		"\t\tmsg = Message(bytearray(data))",
	]
	for index, type_name in enumerate(types):
		struct = structs[type_name]
		layout = struct.layout				# type: ignore[attr-defined]
		fixed  = layout.is_fixed_size and layout.is_byte_sized
		cls    = py_name(type_name)			# type: ignore[operator]
		value  = cell_value(decls[type_name])
		member = next((one for one in decls[type_name].members
		               if isinstance(one, ast.Field) and one.name == value),
		              None)
		view = f"view_{index}"
		lines += [f"\t\tif cell == {index}:	# {type_name}"]
		if fixed:
			lines += [f"\t\t\tif len(data) != {cls}.SIZE_BYTES:",
			          f"\t\t\t\traise BoundsError(\"{type_name} is"
			          f" {{{cls}.SIZE_BYTES}} bytes; {{len(data)}} given\")",
			          f"\t\t\t{view} = {cls}.at(msg, 0)"]
		else:
			lines += [f"\t\t\t{view} = {cls}.at(msg, 0, len(data))"]
		lines += [f"\t\t\t{view}.validate()"]
		if value is None:
			lines += ["\t\t\treturn None"]
		else:
			attribute = py_name(value)			# type: ignore[operator]
			lines += [f"\t\t\treturn int({view}.{attribute})"]
			del member
	lines += ["\t\traise ConstraintError(\"no such cell type\")", ""]
	lines += [
		"\t@staticmethod",
		f"\tdef check(entry: \"{name}.Entry\","
		" data: bytes | bytearray | memoryview) -> None:",
		'\t\t"""Raise where `data` is not a cell of `entry`."""',
		"\t\tif len(data) < entry.min_size or len(data) > entry.max_size:",
		"\t\t\traise ConstraintError(f\"{entry.key}: {len(data)} bytes"
		" is outside the cell's size\")",
		f"\t\theld = {name}._cell(entry.cell, data)",
		"\t\tif held is not None and ((entry.min is not None and held < entry.min)",
		"\t\t                         or (entry.max is not None and held > entry.max)):",
		"\t\t\traise ConstraintError(f\"{entry.key}: {held} is outside its bounds\")",
		"",
		"\t@staticmethod",
		f"\tdef value(entry: \"{name}.Entry\","
		" data: bytes | bytearray | memoryview) -> int | None:",
		'\t\t"""What a cell is worth; None where it is not a cell or has no value."""',
		"\t\ttry:",
		f"\t\t\t{name}.check(entry, data)",
		"\t\texcept SituError:",
		"\t\t\treturn None",
		f"\t\treturn {name}._cell(entry.cell, data)",
		"",
		"\t@staticmethod",
		f"\tdef safer(entry: \"{name}.Entry\", a: bytes | bytearray | memoryview,",
		"\t          b: bytes | bytearray | memoryview) -> bool:",
		'\t\t"""Whether `b` is strictly safer than `a` by the entry\'s direction."""',
		"\t\tif entry.safe not in (SAFE_LOWER, SAFE_HIGHER):",
		"\t\t\treturn False",
		f"\t\tfirst, second = {name}.value(entry, a), {name}.value(entry, b)",
		"\t\tif first is None or second is None:",
		"\t\t\treturn False",
		"\t\treturn second < first if entry.safe == SAFE_LOWER else second > first",
		"",
		"",
		f"{name}.ENTRIES = (",
	]
	for entry in decl.entries:
		safe = _literal(entry, "safe")
		fields = [
			f"key={entry.key!r}", f"family={entry.family}",
			f"safe={ {'lower': 'SAFE_LOWER', 'higher': 'SAFE_HIGHER'}.get(str(safe), '0') }",
			f"min={_literal(entry, 'min')!r}", f"max={_literal(entry, 'max')!r}",
			f"min_size={_literal(entry, 'min_size') or 0!r}",
			f"max_size={_literal(entry, 'max_size') if _attr(entry, 'max_size') else 0xFFFFFFFF!r}",
			f"default={_literal(entry, 'default')!r}",
			f"unit={_literal(entry, 'unit')!r}",
			f"summary={entry.summary!r}",
			f"window_ms={_literal(entry, 'window') or 0!r}",
			f"cell={name}.CELL_{entry.type_name.upper()}",
		]
		for policy in schema.policies():
			held = _attr(entry, policy.name)
			words = _policy_words(held.value) if held is not None else []
			mask = " | ".join(f"{policy.name.upper()}_{word.upper()}"
			                  for word in words or []) or "0"
			fields.append(f"{policy.name}={mask}")
		lines.append(f"\t{name}.Entry({', '.join(fields)}),")
	lines.append(")")
	return lines
