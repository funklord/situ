"""A registry of typed cells, in C (0070, step 3).

What fuzznet hand-writes as `fzn_schema_entry_t` and its parsers, generated
from the schema: a table of entries, and four functions over it --

    `_find`   the entry a key is: exact, or a family's instance;
    `_check`  whether bytes are a cell of the entry: its type's `validate`,
              then the entry's bounds on the bytes and on the value;
    `_value`  what a cell is worth, through its type's own getter;
    `_safer`  whether one value is safer than another by the entry's
              `safe` direction.

The parse is the type's, so what `_check` refuses is exactly what a reader
of the cell would refuse -- the property fuzznet's header asks for in its
own words, "the parsers are the readers' too".
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import c_name, ident, macro
from situc.diagnostics import error
from situc.wellformed import cell_value

#: `safe = lower | higher`, as the table spells it.
SAFE = {None: 0, "lower": 1, "higher": 2}


def _attr(entry: ast.RegistryEntry, name: str) -> ast.Attr | None:
	return next((attr for attr in entry.attrs if attr.name == name), None)


def _c_string(text: str | None) -> str:
	if text is None:
		return "NULL"
	escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
	           .replace("\n", "\\n"))
	return f'"{escaped}"'


def _policy_mask(entry: ast.RegistryEntry, policy: ast.PolicyDecl,
		prefix: str) -> str:
	from situc.wellformed import _policy_words

	held = _attr(entry, policy.name)
	words = _policy_words(held.value) if held is not None else []
	if not words:
		return "0u"
	return " | ".join(macro(prefix, policy.name, word) for word in words)


class Registry:
	"""One registry's C, given the emitter that knows the structs."""

	def __init__(self, emitter: object, decl: ast.RegistryDecl) -> None:
		self.emitter = emitter
		self.decl    = decl
		self.prefix  = emitter.prefix			# type: ignore[attr-defined]
		self.schema  = emitter.schema			# type: ignore[attr-defined]
		self.structs = emitter.resolved.structs	# type: ignore[attr-defined]
		self.types   = list(dict.fromkeys(entry.type_name
		                                  for entry in decl.entries))
		for entry in decl.entries:
			self._held_whole(entry)

	def name(self, *parts: str) -> str:
		return ident(self.prefix, self.decl.name, *parts)

	def _held_whole(self, entry: ast.RegistryEntry) -> None:
		"""A cell is its whole message, so its type has to say where the
		bytes end: by being one size, or by running to the end of its frame.
		A type ending anywhere else would leave bytes after it that no check
		reads -- refused here rather than accepted unread (0070)."""
		struct = self.structs[entry.type_name]
		layout = struct.layout
		if layout.is_fixed_size and layout.is_byte_sized:
			return
		if any(isinstance(placement.length_ast, ast.Remaining)
		       and "." not in placement.path[len(struct.name) + 1:]
		       for placement in layout.placements):
			return
		raise error(f"a cell of `{entry.type_name}` would not end where its "
		            "bytes do", entry.type_span,
		            label = "neither one size nor run to the frame's end",
		            notes = ["a registry's cell is the whole of its bytes, so "
		                     "its type is fixed-size or ends in a "
		                     "`[remaining]` member (0070)"])

	# -- header ----------------------------------------------------------

	def header(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		count   = macro(self.prefix, self.decl.name, "count")
		policies = [policy.name for policy in self.schema.policies()]
		return [
			"",
			f"/* registry {self.decl.name} (0070): a keyspace of typed cells.",
			" * Each entry is a key -- one ending in '/' is a family, whose",
			" * instances add one word of a-z and 0-9 -- and the struct its",
			" * cell's bytes are a message of. */",
			f"#define {count} {len(self.decl.entries)}u",
			"",
			f"typedef struct {self.name('entry')} {{",
			"\tconst char *key;        /* NUL-ended, as written */",
			"\tuint32_t    key_len;",
			"\tuint8_t     family;",
			f"\tuint8_t     safe;       /* {macro(self.prefix, 'safe_lower')}"
			f" or {macro(self.prefix, 'safe_higher')}, or 0 */",
			"\tuint8_t     has_min;",
			"\tuint8_t     has_max;",
			"\tint64_t     min;        /* the value's bounds */",
			"\tint64_t     max;",
			"\tuint32_t    min_size;   /* the bytes' bounds */",
			"\tuint32_t    max_size;",
			"\tconst char *def;        /* as it would be written; NULL for none */",
			"\tconst char *unit;       /* shown, never parsed; NULL for none */",
			"\tconst char *summary;",
			"\tuint32_t    window_ms;  /* written no oftener than this; 0 */",
			*(f"\tuint32_t    {c_name(policy)};" for policy in policies),
			"\tuint32_t    cell;       /* which type, for `_check` */",
			f"}} {entry_t};",
			"",
			f"extern const {entry_t} {self.name('entries')}[{count}];",
			"",
			"/* The entry `key` is, or NULL: exact, or a family's instance. */",
			f"const {entry_t} *{self.name('find')}(const uint8_t *key,"
			" uint32_t len);",
			"",
			"/* SITU_OK where `value` is a cell of `entry`: its type's",
			" * `validate`, then the entry's bounds on its bytes and its value;",
			" * the type's refusal, or SITU_ERR_CONSTRAINT for a bound, where",
			" * not. */",
			f"situ_err_t {self.name('check')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *value, uint32_t len);",
			"",
			"/* What a cell of `entry` is worth, where its type has a value;",
			" * SITU_ERR_CONSTRAINT where it has none, or the bytes are not",
			" * a cell. */",
			f"situ_err_t {self.name('value')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *value, uint32_t len, int64_t *out);",
			"",
			"/* 1 where `b` is strictly safer than `a` by the entry's `safe`",
			" * direction -- of two values written apart, the safer stands --",
			" * and 0 where it is not, the two are equal, the entry states no",
			" * direction, or either is not a cell of it. */",
			f"int {self.name('safer')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *a, uint32_t a_len,",
			"\t\tconst uint8_t *b, uint32_t b_len);",
		]

	# -- source ----------------------------------------------------------

	def source(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		count   = macro(self.prefix, self.decl.name, "count")
		lines = ["", f"/* registry {self.decl.name} (0070) */",
		         f"const {entry_t} {self.name('entries')}[{count}] = {{"]
		for entry in self.decl.entries:
			lines.extend(self._row(entry))
		lines += ["};", ""]
		lines += self._cell()
		lines += self._find()
		lines += self._check()
		lines += self._value()
		lines += self._safer()
		return lines

	def _row(self, entry: ast.RegistryEntry) -> list[str]:
		def number(name: str) -> int | None:
			held = _attr(entry, name)
			return held.value.value if held is not None else None  # type: ignore[union-attr]

		def text(name: str) -> str | None:
			held = _attr(entry, name)
			return held.value.value if held is not None else None  # type: ignore[union-attr]

		safe = _attr(entry, "safe")
		low, high = number("min"), number("max")
		smallest, largest = number("min_size"), number("max_size")
		key = entry.key.encode("utf-8")
		return [
			"\t{",
			f"\t\t.key       = {_c_string(entry.key)},",
			f"\t\t.key_len   = {len(key)}u,",
			f"\t\t.family    = {1 if entry.family else 0}u,",
			f"\t\t.safe      = "
			f"{SAFE[safe.value.name if safe is not None else None]}u,"  # type: ignore[union-attr]
			,
			f"\t\t.has_min   = {0 if low is None else 1}u,",
			f"\t\t.has_max   = {0 if high is None else 1}u,",
			f"\t\t.min       = INT64_C({low or 0}),",
			f"\t\t.max       = INT64_C({high or 0}),",
			f"\t\t.min_size  = {smallest or 0}u,",
			f"\t\t.max_size  = "
			f"{'UINT32_MAX' if largest is None else f'{largest}u'},",
			f"\t\t.def       = {_c_string(text('default'))},",
			f"\t\t.unit      = {_c_string(text('unit'))},",
			f"\t\t.summary   = {_c_string(entry.summary)},",
			f"\t\t.window_ms = {number('window') or 0}u,",
			*(f"\t\t.{c_name(policy.name)} = "
			  f"{_policy_mask(entry, policy, self.prefix)},"
			  for policy in self.schema.policies()),
			f"\t\t.cell      = {self.types.index(entry.type_name)}u,",
			"\t},",
		]

	def _cell(self) -> list[str]:
		"""One function over every type: validate a cell, and read its value
		where it has one. A switch rather than a table of function pointers,
		because the types' views differ in signature."""
		lines = [
			f"static situ_err_t {self.name('cell')}(uint32_t cell,",
			"\t\tconst uint8_t *data, uint32_t len,"
			" int64_t *value, int *has_value)",
			"{",
			"\tsitu_msg_t  msg;",
			"\tsitu_view_t view;",
			"\tsitu_err_t  e;",
			"",
			"\t*has_value = 0;",
			"\t*value     = 0;",
			"\tsitu_msg_init(&msg, (uint8_t *)(uintptr_t)data, len);",
			"\tswitch (cell) {",
		]
		for index, name in enumerate(self.types):
			lines += [f"\tcase {index}u: /* {name} */"]
			lines += [f"\t\t{one}" if one else "" for one in self._one_cell(name)]
		lines += ["\tdefault:", "\t\treturn SITU_ERR_CONSTRAINT;", "\t}", "}", ""]
		return lines

	def _one_cell(self, name: str) -> list[str]:
		struct = self.structs[name]
		decl   = next(one for one in self.schema.structs() if one.name == name)
		layout = struct.layout
		fixed  = layout.is_fixed_size and layout.is_byte_sized
		view   = ident(self.prefix, name, "view")
		lines  = ([f"if (len != {macro(self.prefix, name, 'size_fixed')}) {{",
		           "\treturn SITU_ERR_BOUNDS;", "}",
		           f"e = {view}(&msg, 0u, &view);"] if fixed
		          else [f"e = {view}(&msg, 0u, len, &view);"])
		lines += ["if (e != SITU_OK) {", "\treturn e;", "}",
		          f"e = {ident(self.prefix, name, 'validate')}(view);",
		          "if (e != SITU_OK) {", "\treturn e;", "}"]
		value = cell_value(decl)
		if value is None:
			return lines + ["return SITU_OK;"]
		getter = ident(self.prefix, name, c_name(value), "get")
		member = next((one for one in decl.members
		               if isinstance(one, ast.Field) and one.name == value),
		              None)
		if member is not None and member.radix is not None:
			scalar = member.type_ref.scalar
			assert scalar is not None
			ctype = f"{'int' if scalar.signed else 'uint'}{scalar.bits}_t"
			return lines + ["{", f"\t{ctype} held = 0;", "",
			                f"\te = {getter}(view, &held);",
			                "\tif (e != SITU_OK) {", "\t\treturn e;", "\t}",
			                "\t*value = (int64_t)held;", "}",
			                "*has_value = 1;", "return SITU_OK;"]
		return lines + [f"*value     = (int64_t){getter}(view);",
		                "*has_value = 1;", "return SITU_OK;"]

	def _find(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		count   = macro(self.prefix, self.decl.name, "count")
		return [
			# No `memcmp`: the generated C reaches for no libc, and a
			# freestanding build -- the big-endian cross check is one --
			# has no <string.h> to give it.
			f"static int {self.name('same')}(const uint8_t *a, const char *b,"
			" uint32_t n)",
			"{",
			"\tfor (uint32_t i = 0u; i < n; i++) {",
			"\t\tif (a[i] != (uint8_t)b[i]) {",
			"\t\t\treturn 0;",
			"\t\t}",
			"\t}",
			"\treturn 1;",
			"}",
			"",
			f"const {entry_t} *{self.name('find')}(const uint8_t *key,"
			" uint32_t len)",
			"{",
			"\tif (key == NULL) {",
			"\t\treturn NULL;",
			"\t}",
			f"\tfor (uint32_t i = 0u; i < {count}; i++) {{",
			f"\t\tconst {entry_t} *e = &{self.name('entries')}[i];",
			"",
			"\t\tif (!e->family) {",
			"\t\t\tif (len == e->key_len"
			f" && {self.name('same')}(key, e->key, len)) {{",
			"\t\t\t\treturn e;",
			"\t\t\t}",
			"\t\t\tcontinue;",
			"\t\t}",
			"\t\t/* A family's instance: the key, then one word. */",
			"\t\tif (len <= e->key_len"
			f" || !{self.name('same')}(key, e->key, e->key_len)) {{",
			"\t\t\tcontinue;",
			"\t\t}",
			"\t\tuint32_t at = e->key_len;",
			"\t\tfor (; at < len; at++) {",
			"\t\t\tif (!((key[at] >= 'a' && key[at] <= 'z')",
			"\t\t\t      || (key[at] >= '0' && key[at] <= '9'))) {",
			"\t\t\t\tbreak;",
			"\t\t\t}",
			"\t\t}",
			"\t\tif (at == len) {",
			"\t\t\treturn e;",
			"\t\t}",
			"\t}",
			"\treturn NULL;",
			"}",
			"",
		]

	def _check(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		return [
			f"situ_err_t {self.name('check')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *value, uint32_t len)",
			"{",
			"\tint64_t    held = 0;",
			"\tint        has  = 0;",
			"\tsitu_err_t e;",
			"",
			"\tif (entry == NULL || (value == NULL && len != 0u)) {",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			"\tif (len < entry->min_size || len > entry->max_size) {",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			f"\te = {self.name('cell')}(entry->cell, value, len, &held, &has);",
			"\tif (e != SITU_OK) {",
			"\t\treturn e;",
			"\t}",
			"\tif (has && ((entry->has_min && held < entry->min)",
			"\t            || (entry->has_max && held > entry->max))) {",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			"\treturn SITU_OK;",
			"}",
			"",
		]

	def _value(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		return [
			f"situ_err_t {self.name('value')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *value, uint32_t len, int64_t *out)",
			"{",
			"\tint64_t    held = 0;",
			"\tint        has  = 0;",
			"\tsitu_err_t e;",
			"",
			"\tif (out == NULL) {",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			f"\te = {self.name('check')}(entry, value, len);",
			"\tif (e != SITU_OK) {",
			"\t\treturn e;",
			"\t}",
			f"\t(void){self.name('cell')}(entry->cell, value, len, &held, &has);",
			"\tif (!has) {",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
			"\t*out = held;",
			"\treturn SITU_OK;",
			"}",
			"",
		]

	def _safer(self) -> list[str]:
		entry_t = f"{self.name('entry')}_t"
		return [
			f"int {self.name('safer')}(const {entry_t} *entry,",
			"\t\tconst uint8_t *a, uint32_t a_len,",
			"\t\tconst uint8_t *b, uint32_t b_len)",
			"{",
			"\tint64_t first = 0, second = 0;",
			"",
			"\tif (entry == NULL || entry->safe == 0u",
			f"\t    || {self.name('value')}(entry, a, a_len, &first) != SITU_OK",
			f"\t    || {self.name('value')}(entry, b, b_len, &second) != SITU_OK) {{",
			"\t\treturn 0;",
			"\t}",
			f"\treturn entry->safe == {macro(self.prefix, 'safe_lower')}"
			" ? second < first : second > first;",
			"}",
			"",
		]


def policy_macros(schema: ast.Schema, prefix: str) -> list[str]:
	"""Each declared policy's words as bits, once per header (0070)."""
	lines: list[str] = []
	if not schema.registries():
		return lines
	lines += ["", "/* `safe`: which of two values written apart stands. */",
	          f"#define {macro(prefix, 'safe_lower')}  1u",
	          f"#define {macro(prefix, 'safe_higher')} 2u"]
	for policy in schema.policies():
		lines += ["", f"/* policy {policy.name} */"]
		for bit, word in enumerate(policy.words):
			lines.append(f"#define {macro(prefix, policy.name, word)}"
			             f" 0x{1 << bit:X}u")
	return lines
