"""Matching a reply to its request in Rust (26.97).

Two things Rust decides differently, and neither is spelling.

`match` is a keyword, so the taking half is `take` -- which is the better
name anyway and the one `Option::take` already establishes: it returns what
was pending and leaves nothing behind, which is exactly why a duplicate reply
is refused.

The table holds a `u64` key and a `u32` handle and borrows nothing, so
ownership has no opinion about it. That is worth stating because it is the
first rung where it could have: had the table kept the request's *bytes*, the
borrow checker would have made a caller-supplied slice outlive every call
into it, and the design would have had to change rather than be translated.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.rust.emit import _ident, _pascal, numeric_accessor
from situc.relation import (Read, KeyLayout, KeyStep, ReadBytes, Refused,
                            Side, SubView, after_region, key_layout)
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate", "refusals"]


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	found = []
	for relation in schema.relations():
		try:
			key_layout(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def _gated(resolved: ResolvedSchema, step: Read | ReadBytes) -> str:
	"""The gate's accessor for a member behind it, `_bits` for an enum as
	`numeric_accessor` answers for a clear one."""
	entry = resolved.find(f"{step.struct}.{step.region}.{step.member}")
	enum = (isinstance(step, Read) and entry is not None
	        and entry.placement.type_name in resolved.layout.env.enums)
	return f"{_ident(step.member)}_bits" if enum else _ident(step.member)


def _opened(sides: Side, view: str, gated: bool) -> tuple[list[str], str]:
	"""Where a clear field is read from: the view, or with `[after]` the
	view the gate was opened over, bound once (26.1122)."""
	if not gated:
		return [], view
	if all(getattr(chain[0], "region", None) is not None
	       for chain, _ in sides):
		return [], view
	return [f"\t\tlet {view}_view = {view}.view();"], f"{view}_view"


def _key(sides: Side, view: str, resolved: ResolvedSchema,
		gated: bool = False) -> list[str]:
	lines = ["\t\tlet mut key: u64 = 0;"]
	bound, root = _opened(sides, view, gated)
	lines += bound
	for chain, width in sides:
		source = root
		for step in chain:
			if isinstance(step, Read) and step.region is not None:
				lines.append(f"\t\tkey = (key << {width}) | "
				             f"u64::from({view}.{_gated(resolved, step)}());"
				             f"\t// {step.path}")
			elif isinstance(step, Read):
				# `_bits` where the member is an enum: its getter answers
				# `Option<T>` and `u64::from` has no such impl, so a relation
				# keyed on one did not compile at all.
				read = numeric_accessor(resolved, step.struct, step.member)
				lines.append(f"\t\tkey = (key << {width}) | "
				             f"u64::from({source}.{read}());"
				             f"\t// {step.path}")
			elif isinstance(step, SubView):
				lines.append(f"\t\tlet {_ident(step.member)} = "
				             f"{source}.{_ident(step.member)}()?;")
				source = _ident(step.member)
	return lines


def _key_bytes(sides: Side, view: str, total: int,
		resolved: ResolvedSchema, gated: bool = False) -> list[str]:
	"""The exact-bytes key (0042), Rust's spelling. `[u8; N]` copies and
	compares by value, so the slot needs no other change."""
	lines = [f"\t\tlet mut key = [0u8; {total}];",
	         "\t\tlet mut at = 0usize;"]
	bound, root = _opened(sides, view, gated)
	lines += bound
	for chain, width in sides:
		source = root
		for step in chain:
			behind = getattr(step, "region", None) is not None
			if isinstance(step, ReadBytes):
				count = width // 8
				lines += [
					f"\t\tkey[at..at + {count}].copy_from_slice("
					f"{view if behind else source}."
					f"{_gated(resolved, step) if behind else _ident(step.member)}"
					f"());\t// {step.path}",
					f"\t\tat += {count};",
				]
			elif isinstance(step, Read):
				count = (width + 7) // 8
				read = (_gated(resolved, step) if behind else
				        numeric_accessor(resolved, step.struct, step.member))
				source = view if behind else source
				lines += [
					f"\t\tlet v = u64::from({source}."
					f"{read}());\t// {step.path}",
					f"\t\tfor b in 0..{count} {{",
					f"\t\t\tkey[at + b] = (v >> (8 * ({count} - 1 - b))) as u8;",
					"\t\t}",
					f"\t\tat += {count};",
				]
			elif isinstance(step, SubView):
				lines.append(f"\t\tlet {_ident(step.member)} = "
				             f"{source}.{_ident(step.member)}()?;")
				source = _ident(step.member)
	lines.append("\t\tlet _ = at;")
	return lines


def key_function(request: Side, layout: KeyLayout) -> list[str]:
	"""`key(...)`: the key a request with these values would have, packed
	as `record_from` packs one it reads (fuzznet, inbox 20261121T0600Z).
	C's `_key`; a part is named by its path below the request."""
	def part(chain: list[KeyStep]) -> str:
		return _ident(chain[-1].path.split(".", 1)[1].replace(".", "_"))
	if layout.packed:
		lines = ["\t\tlet mut key: u64 = 0;"]
		for chain, width in request:
			cut = (part(chain) if width >= 64
			       else f"({part(chain)} & {hex((1 << width) - 1)})")
			lines.append(f"\t\tkey = (key << {width}) | {cut};")
		joined = ", ".join(f"{part(chain)}: u64" for chain, _ in request)
		return ["\t/// The key a request with these values would have.",
		        f"\tpub fn key({joined}) -> u64 {{", *lines, "\t\tkey",
		        "\t}", ""]
	lines = [f"\t\tlet mut key = [0u8; {layout.total_bytes}];",
	         "\t\tlet mut at = 0usize;"]
	params = []
	for chain, width in request:
		if isinstance(chain[-1], ReadBytes):
			params.append(f"{part(chain)}: &[u8; {width // 8}]")
			lines += [f"\t\tkey[at..at + {width // 8}].copy_from_slice({part(chain)});",
			          f"\t\tat += {width // 8};"]
		else:
			count = (width + 7) // 8
			params.append(f"{part(chain)}: u64")
			lines += [f"\t\tfor b in 0..{count} {{",
			          f"\t\t\tkey[at + b] = ({part(chain)} >> (8 * ({count} - 1 - b))) as u8;",
			          "\t\t}", f"\t\tat += {count};"]
	return ["\t/// The key a request with these values would have.",
	        f"\tpub fn key({', '.join(params)}) -> [u8; {layout.total_bytes}] {{",
	        *lines, "\t\tlet _ = at;", "\t\tkey", "\t}", ""]


def reply_type(relation: ast.Relation) -> str:
	"""What the reply side takes: its view, or with `[after]` the gate of
	that sealed region, which only a verified open hands out (26.1122)."""
	region = after_region(relation)
	struct = relation.params[1].type_name
	return (f"{_pascal(f'{struct}_{region}_gate')}<'_>" if region
	        else _pascal(struct))


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	ready = []
	for relation in schema.relations():
		try:
			ready.append((relation, key_layout(relation, resolved)))
		except Refused:
			continue
	if not ready:
		return {}

	lines = [
		f"// Generated by situc {__version__} from {basename}.situ -- do not edit.",
		"//",
		"// Conversations: a pending table per relation, matching a reply to",
		"// the request it answers. The schema states the pairing and this",
		"// holds the set. A separate module -- a rung adds files rather than",
		"// changing them (decision 0032).",
		"",
		f"use crate::{basename}::*;",
		"use crate::situ_rt::{Error, Result};",
		"",
	]

	for relation, layout in ready:
		request, response = layout.request, layout.response
		held  = _pascal(relation.name)
		first, second = relation.params
		region = after_region(relation)
		reply  = reply_type(relation)
		asked = (_key(request, first.name, resolved) if layout.packed
		         else _key_bytes(request, first.name, layout.total_bytes,
		                         resolved))
		heard = (_key(response, second.name, resolved, bool(region))
		         if layout.packed
		         else _key_bytes(response, second.name, layout.total_bytes,
		                         resolved, bool(region)))
		lines += [
			f"/// Pending requests for `{relation.name}`.",
			"///",
			"/// The slots are yours and so is their number: a full table",
			"/// answers `Error::Bounds` rather than evicting an exchange you",
			"/// still wanted. Nothing is allocated.",
			"///",
			"/// `peer` is yours to number too -- a session, the context that",
			"/// verifies the reply -- and only a reply taken with the same one",
			"/// answers (26.1122). The calls without `_from` hold peer 0.",
			"#[derive(Clone, Copy, Default)]",
			f"pub struct {held}Slot {{",
			("\tkey: u64," if layout.packed
			 else f"\tkey: [u8; {layout.total_bytes}],"),
			"\tid: u32,",
			"\tpeer: u32,",
			"\tlive: bool,",
			"}",
			"",
			f"pub struct {held}Table<'a> {{",
			f"\tslots: &'a mut [{held}Slot],",
			"}",
			"",
			f"impl<'a> {held}Table<'a> {{",
			f"\tpub fn new(slots: &'a mut [{held}Slot]) -> Self {{",
			"\t\tfor slot in slots.iter_mut() {",
			"\t\t\tslot.live = false;",
			"\t\t}",
			"\t\tSelf { slots }",
			"\t}",
			"",
			f"\t/// Remember `{first.name}` under its key and `peer`.",
			*key_function(request, layout),
			"\t/// `record_from` by a key from `key(...)`, no request in hand.",
			("\tpub fn record_key_from(&mut self, key: u64, peer: u32, id: u32)"
			 " -> Result<()> {" if layout.packed else
			 f"\tpub fn record_key_from(&mut self, key: [u8; {layout.total_bytes}],"
			 " peer: u32, id: u32) -> Result<()> {"),
			"\t\tfor slot in self.slots.iter_mut() {",
			"\t\t\tif !slot.live {",
			"\t\t\t\t*slot = Self::held(key, id, peer);",
			"\t\t\t\treturn Ok(());",
			"\t\t\t}",
			"\t\t}",
			"\t\tErr(Error::Bounds)",
			"\t}",
			"",
			f"\tpub fn record_from(&mut self, {first.name}: &"
			f"{_pascal(first.type_name)}, peer: u32, id: u32) -> Result<()> {{",
			*asked,
			"\t\tself.record_key_from(key, peer, id)",
			"\t}",
			"",
			f"\t/// Remember `{first.name}` under its key.",
			f"\tpub fn record(&mut self, {first.name}: &"
			f"{_pascal(first.type_name)}, id: u32) -> Result<()> {{",
			f"\t\tself.record_from({first.name}, 0, id)",
			"\t}",
			"",
			f"\t/// Take the request `{second.name}` answers, and forget it.",
			"\t///",
			"\t/// `take` rather than `match`, which is a keyword -- and the",
			"\t/// better name: it leaves nothing behind, which is why a",
			"\t/// duplicate reply answers `Error::Constraint`.",
			*([f"\t///",
			   f"\t/// Handed `{region}`'s gate: only a verified reply ends an",
			   "\t/// exchange (26.1122)."] if region else []),
			f"\tpub fn take_from(&mut self, {second.name}: &{reply}, "
			"peer: u32) -> Result<u32> {",
			*heard,
			"",
			"\t\tfor slot in self.slots.iter_mut() {",
			"\t\t\tif slot.live && slot.peer == peer && slot.key == key {",
			"\t\t\t\tslot.live = false;",
			"\t\t\t\treturn Ok(slot.id);",
			"\t\t\t}",
			"\t\t}",
			"\t\tErr(Error::Constraint)",
			"\t}",
			"",
			f"\tpub fn take(&mut self, {second.name}: &{reply}) -> Result<u32> {{",
			f"\t\tself.take_from({second.name}, 0)",
			"\t}",
			"",
			"\t/// `take` without taking: which request a reply answers, left",
			"\t/// pending for a reply that comes in pieces (26.1009).",
			f"\tpub fn peek_from(&self, {second.name}: &{reply}, peer: u32)"
			" -> Result<u32> {",
			*heard,
			"",
			"\t\tfor slot in self.slots.iter() {",
			"\t\t\tif slot.live && slot.peer == peer && slot.key == key {",
			"\t\t\t\treturn Ok(slot.id);",
			"\t\t\t}",
			"\t\t}",
			"\t\tErr(Error::Constraint)",
			"\t}",
			"",
			f"\tpub fn peek(&self, {second.name}: &{reply}) -> Result<u32> {{",
			f"\t\tself.peek_from({second.name}, 0)",
			"\t}",
			"",
			(f"\tfn held(key: u64, id: u32, peer: u32) -> {held}Slot {{"
			 if layout.packed else
			 f"\tfn held(key: [u8; {layout.total_bytes}], id: u32, peer: u32)"
			 f" -> {held}Slot {{"),
			f"\t\t{held}Slot {{ key, id, peer, live: true }}",
			"\t}",
			"}",
			"",
		]

	return {f"{basename}_converse.rs": "\n".join(lines).rstrip("\n") + "\n"}
