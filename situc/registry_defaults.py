"""A registry's defaults, each held to be a cell of its type (0070).

A `default` is written as it would be stored, so it is a message of the
cell's type like any other value: `"14d"` for a duration, `"off"` for a
flag. Checked at compile time by the registry's own `check`, through the
Python accessors built in memory as `situc verify` builds them -- the same
parse every reader runs -- so a default no reader would accept is refused
where it is written rather than met by the first program that falls back
to it.
"""

from __future__ import annotations

from situc import ast
from situc.diagnostics import error
from situc.resolve import ResolvedSchema


def check(schema: ast.Schema, resolved: ResolvedSchema) -> None:
	"""Refuse a registry entry whose `default` is not a cell of it."""
	pending = [(registry, row, attr)
	           for registry in schema.registries()
	           for row, entry in enumerate(registry.entries)
	           for attr in entry.attrs
	           if attr.name == "default"
	           and isinstance(attr.value, ast.StringLiteral)]
	if not pending:
		return

	from situc.codegen.python.emit import py_name
	from situc.verify import _module

	module  = _module(schema, resolved, "registry_defaults")
	refusal = module.__situ_refusal__			# type: ignore[attr-defined]
	for registry, row, attr in pending:
		held  = getattr(module, py_name(registry.name))
		entry = held.ENTRIES[row]
		value = attr.value.value.encode("ascii", "replace")  # type: ignore[union-attr]
		try:
			held.check(entry, value)
		except refusal as why:
			raise error(
				f"`default` is not a cell of `{registry.entries[row].key}`",
				attr.span,
				label = f"its type refuses {attr.value.value!r}",  # type: ignore[union-attr]
				notes = [f"{type(why).__name__}: {why}",
				         "a default is written as it would be stored, so "
				         "it is held to the cell's type and bounds as any "
				         "value is (0070)"]) from None
