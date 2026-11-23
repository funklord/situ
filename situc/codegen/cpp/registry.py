"""A registry of typed cells, in C++ (0070, step 6).

The C registry (26.1081) as a class: a nested `entry`, the table as a
C++17 inline static, and `find`, `check`, `value` and `safer` as static
member functions returning `::situ::rt::err` where C returns `situ_err_t`.
Header-only, as every other C++ accessor is, and `noexcept` throughout --
the tests build with `-fno-exceptions`.
"""

from __future__ import annotations

from collections.abc import Mapping

from situc import ast
from situc.codegen.c.names import c_name
from situc.codegen.cpp.names import class_name
from situc.wellformed import _policy_words, cell_value


def _attr(entry: ast.RegistryEntry, name: str) -> ast.Attr | None:
	return next((attr for attr in entry.attrs if attr.name == name), None)


def _number(entry: ast.RegistryEntry, name: str) -> int | None:
	held = _attr(entry, name)
	return held.value.value if held is not None else None  # type: ignore[union-attr]


def _string(text: str | None) -> str:
	if text is None:
		return "nullptr"
	escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
	           .replace("\n", "\\n"))
	return f'"{escaped}"'


def policy_constants(schema: ast.Schema) -> list[str]:
	"""Each declared policy's words as bits, once per header."""
	if not schema.registries():
		return []
	lines = ["", "/* `safe`: which of two values written apart stands (0070). */",
	         "inline constexpr std::uint8_t safe_lower  = 1u;",
	         "inline constexpr std::uint8_t safe_higher = 2u;"]
	for policy in schema.policies():
		lines += ["", f"/* policy {policy.name} */"]
		for bit, word in enumerate(policy.words):
			lines.append(f"inline constexpr std::uint32_t "
			             f"{c_name(policy.name)}_{c_name(word)} = 0x{1 << bit:X}u;")
	return lines


def registry_class(schema: ast.Schema, decl: ast.RegistryDecl,
		structs: Mapping[str, object], namespace: str) -> list[str]:
	name     = c_name(decl.name)
	types    = list(dict.fromkeys(entry.type_name for entry in decl.entries))
	policies = [c_name(policy.name) for policy in schema.policies()]
	decls    = {one.name: one for one in schema.structs()}
	err      = "::situ::rt::err"
	lines = [
		"",
		f"/** registry {decl.name} (0070): a keyspace of typed cells. A key",
		" *  ending in '/' is a family, whose instances add one word of a-z",
		" *  and 0-9; each cell is a message of its type, checked by that",
		" *  type's own `validate` and then by the entry's bounds. */",
		f"class {name} {{",
		"public:",
		"\tstruct entry {",
		"\t\tconst char   *key;",
		"\t\tstd::uint32_t key_len;",
		"\t\tbool          family;",
		"\t\tstd::uint8_t  safe;",
		"\t\tbool          has_min;",
		"\t\tbool          has_max;",
		"\t\tstd::int64_t  min;",
		"\t\tstd::int64_t  max;",
		"\t\tstd::uint32_t min_size;",
		"\t\tstd::uint32_t max_size;",
		"\t\tconst char   *def;",
		"\t\tconst char   *unit;",
		"\t\tconst char   *summary;",
		"\t\tstd::uint32_t window_ms;",
		*(f"\t\tstd::uint32_t {policy};" for policy in policies),
		"\t\tstd::uint32_t cell;",
		"\t};",
		"",
		f"\tstatic constexpr std::uint32_t count = {len(decl.entries)}u;",
		"",
		"\t/* Which type a cell is: an entry's `cell`, by name (26.1169). */",
		*(f"\tstatic constexpr std::uint32_t cell_{c_name(type_name)} = "
		  f"{index}u;" for index, type_name in enumerate(types)),
		"",
		"\tstatic inline const entry entries[count] = {",
	]
	for entry in decl.entries:
		low, high = _number(entry, "min"), _number(entry, "max")
		largest   = _number(entry, "max_size")
		safe      = _attr(entry, "safe")
		safe_name = getattr(safe.value, "name", None) if safe else None
		masks = []
		for policy in schema.policies():
			held  = _attr(entry, policy.name)
			words = _policy_words(held.value) if held is not None else []
			masks.append(" | ".join(f"{c_name(policy.name)}_{c_name(word)}"
			                        for word in words or []) or "0u")
		default = _attr(entry, "default")
		unit    = _attr(entry, "unit")
		lines.append(
			"\t\t{" + ", ".join([
				_string(entry.key), f"{len(entry.key.encode())}u",
				"true" if entry.family else "false",
				{"lower": "safe_lower", "higher": "safe_higher"}.get(
					str(safe_name), "0u"),
				"false" if low is None else "true",
				"false" if high is None else "true",
				f"INT64_C({low or 0})", f"INT64_C({high or 0})",
				f"{_number(entry, 'min_size') or 0}u",
				"0xFFFFFFFFu" if largest is None else f"{largest}u",
				_string(default.value.value if default else None),  # type: ignore[union-attr]
				_string(unit.value.value if unit else None),  # type: ignore[union-attr]
				_string(entry.summary),
				f"{_number(entry, 'window') or 0}u",
				*masks,
				f"cell_{c_name(entry.type_name)}",
			]) + "},")
	lines += [
		"\t};",
		"",
		"\t/** The entry `key` is: exact, or a family's instance. */",
		"\t[[nodiscard]] static const entry *find(const std::uint8_t *key,",
		"\t\t\tstd::uint32_t len) noexcept",
		"\t{",
		"\t\tif (key == nullptr) {",
		"\t\t\treturn nullptr;",
		"\t\t}",
		"\t\tfor (const entry &e : entries) {",
		"\t\t\tif (!e.family) {",
		"\t\t\t\tif (len == e.key_len && same(key, e.key, len)) {",
		"\t\t\t\t\treturn &e;",
		"\t\t\t\t}",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tif (len <= e.key_len || !same(key, e.key, e.key_len)) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tstd::uint32_t at = e.key_len;",
		"\t\t\tfor (; at < len; at++) {",
		"\t\t\t\tif (!((key[at] >= 'a' && key[at] <= 'z')",
		"\t\t\t\t      || (key[at] >= '0' && key[at] <= '9'))) {",
		"\t\t\t\t\tbreak;",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t\tif (at == len) {",
		"\t\t\t\treturn &e;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn nullptr;",
		"\t}",
		"",
		"\t/** ok where `data` is a cell of `e`; the type's refusal, or",
		"\t *  constraint for a bound, where not. */",
		f"\t[[nodiscard]] static {err} check(const entry &e,",
		"\t\t\tconst std::uint8_t *data, std::uint32_t len) noexcept",
		"\t{",
		"\t\tstd::int64_t held = 0;",
		"\t\tbool         has  = false;",
		"",
		"\t\tif (len < e.min_size || len > e.max_size) {",
		f"\t\t\treturn {err}::constraint;",
		"\t\t}",
		f"\t\tconst {err} r = cell(e.cell, data, len, held, has);",
		f"\t\tif (r != {err}::ok) {{",
		"\t\t\treturn r;",
		"\t\t}",
		"\t\tif (has && ((e.has_min && held < e.min)",
		"\t\t            || (e.has_max && held > e.max))) {",
		f"\t\t\treturn {err}::constraint;",
		"\t\t}",
		f"\t\treturn {err}::ok;",
		"\t}",
		"",
		"\t/** What a cell of `e` is worth; constraint where it is not a cell",
		"\t *  or has no value. */",
		f"\t[[nodiscard]] static {err} value(const entry &e,",
		"\t\t\tconst std::uint8_t *data, std::uint32_t len,",
		"\t\t\tstd::int64_t &out) noexcept",
		"\t{",
		"\t\tbool has = false;",
		"",
		f"\t\tif (check(e, data, len) != {err}::ok) {{",
		f"\t\t\treturn {err}::constraint;",
		"\t\t}",
		"\t\t(void)cell(e.cell, data, len, out, has);",
		f"\t\treturn has ? {err}::ok : {err}::constraint;",
		"\t}",
		"",
		"\t/** Whether `b` is strictly safer than `a` by the entry's direction. */",
		"\t[[nodiscard]] static bool safer(const entry &e,",
		"\t\t\tconst std::uint8_t *a, std::uint32_t a_len,",
		"\t\t\tconst std::uint8_t *b, std::uint32_t b_len) noexcept",
		"\t{",
		"\t\tstd::int64_t first = 0, second = 0;",
		"",
		"\t\tif (e.safe == 0u",
		f"\t\t    || value(e, a, a_len, first) != {err}::ok",
		f"\t\t    || value(e, b, b_len, second) != {err}::ok) {{",
		"\t\t\treturn false;",
		"\t\t}",
		"\t\treturn e.safe == safe_lower ? second < first : second > first;",
		"\t}",
		"",
		"private:",
		"\tstatic bool same(const std::uint8_t *a, const char *b,",
		"\t\t\tstd::uint32_t n) noexcept",
		"\t{",
		"\t\tfor (std::uint32_t i = 0u; i < n; i++) {",
		"\t\t\tif (a[i] != static_cast<std::uint8_t>(b[i])) {",
		"\t\t\t\treturn false;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn true;",
		"\t}",
		"",
		f"\tstatic {err} cell(std::uint32_t which, const std::uint8_t *data,",
		"\t\t\tstd::uint32_t len, std::int64_t &value, bool &has) noexcept",
		"\t{",
		"\t\tstatic std::uint8_t none = 0u;",
		"\t\t::situ::rt::message msg(data != nullptr",
		"\t\t\t? const_cast<std::uint8_t *>(data) : &none, len);",
		"",
		"\t\tvalue = 0;",
		"\t\thas   = false;",
		"\t\tswitch (which) {",
	]
	for index, type_name in enumerate(types):
		struct = structs[type_name]
		layout = struct.layout				# type: ignore[attr-defined]
		fixed  = layout.is_fixed_size and layout.is_byte_sized
		# Qualified: inside the class, `count` is its own size.
		cls    = f"::{namespace}::{class_name(struct)}"	# type: ignore[arg-type]
		value  = cell_value(decls[type_name])
		member = next((one for one in decls[type_name].members
		               if isinstance(one, ast.Field) and one.name == value),
		              None)
		lines += [f"\t\tcase {index}u: {{ /* {type_name} */",
		          f"\t\t\t{cls} view;"]
		if fixed:
			lines += [f"\t\t\tif (len != {cls}::size_bytes) {{",
			          f"\t\t\t\treturn {err}::bounds;", "\t\t\t}",
			          f"\t\t\t{err} r = {cls}::at(msg, 0u, view);"]
		else:
			lines += [f"\t\t\t{err} r = {cls}::at(msg, 0u, len, view);"]
		lines += [f"\t\t\tif (r != {err}::ok) {{", "\t\t\t\treturn r;", "\t\t\t}",
		          "\t\t\tr = view.validate();",
		          f"\t\t\tif (r != {err}::ok) {{", "\t\t\t\treturn r;", "\t\t\t}"]
		if value is not None:
			getter = c_name(value)
			if member is not None and member.radix is not None:
				scalar = member.type_ref.scalar
				assert scalar is not None
				ctype = f"std::{'int' if scalar.signed else 'uint'}{scalar.bits}_t"
				lines += [f"\t\t\t{ctype} held{{}};",
				          f"\t\t\tr = view.{getter}(held);",
				          f"\t\t\tif (r != {err}::ok) {{", "\t\t\t\treturn r;",
				          "\t\t\t}",
				          "\t\t\tvalue = static_cast<std::int64_t>(held);"]
			else:
				lines += ["\t\t\tvalue = static_cast<std::int64_t>"
				          f"(view.{getter}());"]
			lines += ["\t\t\thas = true;"]
		lines += [f"\t\t\treturn {err}::ok;", "\t\t}"]
	lines += ["\t\tdefault:", f"\t\t\treturn {err}::constraint;", "\t\t}",
	          "\t}", "};"]
	return lines
