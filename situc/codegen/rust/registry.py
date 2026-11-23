"""A registry of typed cells, in Rust (0070, step 6).

The C registry (26.1081) in Rust's idiom: an `<Name>Entry` struct, a unit
struct `<Name>` whose `ENTRIES` is the table, and `find`, `check`, `value`
and `safer` as associated functions. `check` is `Result<()>` -- the cell
type's own error where the bytes are not a cell, `Error::Constraint` for a
stated bound -- and `value` is `Result<i64>`, `Error::Constraint` where the
cell has no value. Everything is `pub` and used, so `-D warnings` holds.
"""

from __future__ import annotations

from collections.abc import Mapping

from situc import ast
from situc.wellformed import _policy_words, cell_value


def _attr(entry: ast.RegistryEntry, name: str) -> ast.Attr | None:
	return next((attr for attr in entry.attrs if attr.name == name), None)


def _number(entry: ast.RegistryEntry, name: str) -> int | None:
	held = _attr(entry, name)
	return held.value.value if held is not None else None  # type: ignore[union-attr]


def _text(text: str | None) -> str:
	if text is None:
		return "None"
	escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
	return f'Some("{escaped}")'


def policy_constants(schema: ast.Schema) -> list[str]:
	"""Each declared policy's words as bits, once per module."""
	if not schema.registries():
		return []
	lines = ["", "/// `safe`: which of two values written apart stands (0070).",
	         "pub const SAFE_LOWER: u8 = 1;", "pub const SAFE_HIGHER: u8 = 2;"]
	for policy in schema.policies():
		lines += ["", f"// policy {policy.name}"]
		for bit, word in enumerate(policy.words):
			lines.append(f"pub const {policy.name.upper()}_{word.upper()}: u32"
			             f" = 0x{1 << bit:X};")
	return lines


def registry_items(schema: ast.Schema, decl: ast.RegistryDecl,
		structs: Mapping[str, object], pascal: object) -> list[str]:
	name     = pascal(decl.name)				# type: ignore[operator]
	entry_t  = f"{name}Entry"
	types    = list(dict.fromkeys(one.type_name for one in decl.entries))
	policies = [policy.name for policy in schema.policies()]
	decls    = {one.name: one for one in schema.structs()}
	lines = [
		"",
		f"/// One cell of registry `{decl.name}` (0070): its key, bounds and",
		"/// what a client shows.",
		f"pub struct {entry_t} {{",
		"\tpub key: &'static str,",
		"\tpub family: bool,",
		"\tpub safe: u8,",
		"\tpub min: Option<i64>,",
		"\tpub max: Option<i64>,",
		"\tpub min_size: usize,",
		"\tpub max_size: usize,",
		"\tpub default: Option<&'static str>,",
		"\tpub unit: Option<&'static str>,",
		"\tpub summary: Option<&'static str>,",
		"\tpub window_ms: u32,",
		*(f"\tpub {policy}: u32," for policy in policies),
		"\tpub cell: u32,",
		"}",
		"",
		f"/// registry `{decl.name}` (0070): a keyspace of typed cells. A key",
		"/// ending in '/' is a family, whose instances add one word of a-z",
		"/// and 0-9; each cell is a message of its type, checked by that",
		"/// type's own `validate` and then by the entry's bounds.",
		f"pub struct {name};",
		"",
		f"impl {name} {{",
		*(line for index, type_name in enumerate(types) for line in (
			f"\t/// A cell of `{type_name}`: an entry's `cell`, by name"
			" (26.1169).",
			f"\tpub const CELL_{type_name.upper()}: u32 = {index};")),
		f"\tpub const ENTRIES: &'static [{entry_t}] = &[",
	]
	for entry in decl.entries:
		safe  = _attr(entry, "safe")
		low   = _number(entry, "min")
		high  = _number(entry, "max")
		large = _number(entry, "max_size")
		default = _attr(entry, "default")
		unit    = _attr(entry, "unit")
		fields = [
			f"key: {entry.key!r}".replace("'", '"'),
			f"family: {'true' if entry.family else 'false'}",
			"safe: " + {"lower": "SAFE_LOWER", "higher": "SAFE_HIGHER"}.get(
				str(getattr(safe.value, "name", "")) if safe else "", "0"),
			f"min: {'None' if low is None else f'Some({low})'}",
			f"max: {'None' if high is None else f'Some({high})'}",
			f"min_size: {_number(entry, 'min_size') or 0}",
			f"max_size: {'usize::MAX' if large is None else large}",
			f"default: {_text(default.value.value if default else None)}",  # type: ignore[union-attr]
			f"unit: {_text(unit.value.value if unit else None)}",  # type: ignore[union-attr]
			f"summary: {_text(entry.summary)}",
			f"window_ms: {_number(entry, 'window') or 0}",
		]
		for policy in schema.policies():
			held  = _attr(entry, policy.name)
			words = _policy_words(held.value) if held is not None else []
			fields.append(f"{policy.name}: " + (" | ".join(
				f"{policy.name.upper()}_{word.upper()}" for word in words or [])
				or "0"))
		fields.append(f"cell: {name}::CELL_{entry.type_name.upper()}")
		lines.append(f"\t\t{entry_t} {{ {', '.join(fields)} }},")
	lines += [
		"\t];",
		"",
		"\t/// The entry `key` is: exact, or a family's instance.",
		f"\tpub fn find(key: &[u8]) -> Option<&'static {entry_t}> {{",
		"\t\tSelf::ENTRIES.iter().find(|e| {",
		"\t\t\tlet held = e.key.as_bytes();",
		"\t\t\tif !e.family {",
		"\t\t\t\treturn key == held;",
		"\t\t\t}",
		"\t\t\tkey.len() > held.len() && key.starts_with(held)",
		"\t\t\t\t&& key[held.len()..].iter()",
		"\t\t\t\t\t.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())",
		"\t\t})",
		"\t}",
		"",
		"\t/// `Ok` where `data` is a cell of `e`; the type's refusal, or",
		"\t/// `Error::Constraint` for a bound, where not.",
		f"\tpub fn check(e: &{entry_t}, data: &[u8]) -> Result<()> {{",
		"\t\tif data.len() < e.min_size || data.len() > e.max_size {",
		"\t\t\treturn Err(Error::Constraint);",
		"\t\t}",
		"\t\tif let Some(held) = Self::cell(e.cell, data)? {",
		"\t\t\tif e.min.is_some_and(|low| held < low)",
		"\t\t\t\t|| e.max.is_some_and(|high| held > high) {",
		"\t\t\t\treturn Err(Error::Constraint);",
		"\t\t\t}",
		"\t\t}",
		"\t\tOk(())",
		"\t}",
		"",
		"\t/// What a cell of `e` is worth; `Error::Constraint` where it is not",
		"\t/// a cell or has no value.",
		f"\tpub fn value(e: &{entry_t}, data: &[u8]) -> Result<i64> {{",
		"\t\tSelf::check(e, data).map_err(|_| Error::Constraint)?;",
		"\t\tSelf::cell(e.cell, data)?.ok_or(Error::Constraint)",
		"\t}",
		"",
		"\t/// Whether `b` is strictly safer than `a` by the entry's direction.",
		f"\tpub fn safer(e: &{entry_t}, a: &[u8], b: &[u8]) -> bool {{",
		"\t\tif e.safe == 0 {",
		"\t\t\treturn false;",
		"\t\t}",
		"\t\tmatch (Self::value(e, a), Self::value(e, b)) {",
		"\t\t\t(Ok(first), Ok(second)) =>",
		"\t\t\t\tif e.safe == SAFE_LOWER { second < first } else { second > first },",
		"\t\t\t_ => false,",
		"\t\t}",
		"\t}",
		"",
		"\tfn cell(which: u32, data: &[u8]) -> Result<Option<i64>> {",
		"\t\tmatch which {",
	]
	for index, type_name in enumerate(types):
		struct = structs[type_name]
		layout = struct.layout				# type: ignore[attr-defined]
		fixed  = layout.is_fixed_size and layout.is_byte_sized
		view_t = pascal(type_name)			# type: ignore[operator]
		value  = cell_value(decls[type_name])
		member = next((one for one in decls[type_name].members
		               if isinstance(one, ast.Field) and one.name == value),
		              None)
		lines += [f"\t\t\t{index} => {{ // {type_name}"]
		if fixed:
			lines += [f"\t\t\t\tif data.len() != {view_t}::SIZE {{",
			          "\t\t\t\t\treturn Err(Error::Bounds);", "\t\t\t\t}"]
		lines += [f"\t\t\t\tlet view = {view_t}::new(data)?;",
		          "\t\t\t\tview.validate()?;"]
		if value is None:
			lines += ["\t\t\t\tOk(None)"]
		else:
			from situc.codegen.rust.emit import _ident
			from situc.codegen.c.names import c_name
			getter = _ident(c_name(value))
			if member is not None and member.radix is not None:
				lines += [f"\t\t\t\tOk(Some(view.{getter}()? as i64))"]
			else:
				lines += [f"\t\t\t\tOk(Some(view.{getter}() as i64))"]
		lines += ["\t\t\t}"]
	lines += ["\t\t\t_ => Err(Error::Constraint),", "\t\t}", "\t}", "}"]
	return lines
