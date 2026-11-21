"""Matching a reply to its request in C++ (26.97).

The C table's design, with the parameters typed: `record` takes the request's
own class rather than a bare view, so the two halves of an exchange cannot be
passed the wrong way round. C cannot do that -- every view there is a
`situ_view_t` -- and it is the same trade the C++ predicate already makes.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import c_name
from situc.codegen.cpp.names import class_name
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


def _opened(sides: Side, view: str, resolved: ResolvedSchema,
		gated: str | None) -> tuple[list[str], str]:
	"""Where a clear field is read from: the view, or with `[after]` the
	view the gate was opened over, bound once (26.1122). `gated` is the
	reply's struct."""
	if gated is None or all(getattr(chain[0], "region", None) is not None
	                        for chain, _ in sides):
		return [], view
	held = class_name(resolved.structs[gated])
	return ([f"\t\tconst ::situ::{held} {view}_view = {view}.view();"],
	        f"{view}_view")


def _key(sides: Side, view: str, resolved: ResolvedSchema,
		gated: str | None = None) -> list[str]:
	lines = ["\t\tstd::uint64_t key = 0u;"]
	bound, root = _opened(sides, view, resolved, gated)
	lines += bound
	for chain, width in sides:
		source = root
		for step in chain:
			if getattr(step, "region", None) is not None:
				# Through the gate's own accessor.
				source = view
			if isinstance(step, Read):
				lines.append(f"\t\tkey = (key << {width}u) | "
				             f"static_cast<std::uint64_t>("
				             f"{source}.{c_name(step.member)}());"
				             f"\t/* {step.path} */")
			elif isinstance(step, SubView):
				held  = class_name(resolved.structs[step.into])
				# Named after the binding, not the member: two key parts
				# through one nested struct (`h.msg`, `h.sender`) declared
				# `held_h` twice and the header did not compile.
				local = f"held_{c_name(step.member)}{step.target}"
				lines += [
					f"\t\t::situ::{held} {local};",
					f"\t\tif ({source}.{c_name(step.member)}({local}) != "
					"::situ::rt::err::ok) {",
					"\t\t\treturn ::situ::rt::err::bounds;",
					"\t\t}",
				]
				source = local
	return lines


def _key_bytes(sides: Side, view: str, resolved: ResolvedSchema,
		total: int, gated: str | None = None) -> list[str]:
	"""The exact-bytes key (0042), C++'s spelling: byte loops rather than
	<cstring>, because this runtime is deliberately freestanding."""
	lines = [f"\t\tstd::uint8_t key[{total}u];",
	         "\t\tstd::uint32_t at = 0u;"]
	bound, root = _opened(sides, view, resolved, gated)
	lines += bound
	for chain, width in sides:
		source = root
		for step in chain:
			if getattr(step, "region", None) is not None:
				source = view
			if isinstance(step, ReadBytes):
				count = width // 8
				lines += [
					f"\t\tconst ::situ::rt::bytes b_{step.target} = "
					f"{source}.{c_name(step.member)}();\t/* {step.path} */",
					f"\t\tfor (std::uint32_t b = 0u; b < {count}u; b++) {{",
					f"\t\t\tkey[at + b] = b_{step.target}[b];",
					"\t\t}",
					f"\t\tat += {count}u;",
				]
			elif isinstance(step, Read):
				count = (width + 7) // 8
				lines += [
					f"\t\tconst std::uint64_t v_{step.target} = "
					f"static_cast<std::uint64_t>("
					f"{source}.{c_name(step.member)}());\t/* {step.path} */",
					f"\t\tfor (std::uint32_t b = 0u; b < {count}u; b++) {{",
					f"\t\t\tkey[at + b] = static_cast<std::uint8_t>("
					f"v_{step.target} >> (8u * ({count}u - 1u - b)));",
					"\t\t}",
					f"\t\tat += {count}u;",
				]
			elif isinstance(step, SubView):
				held  = class_name(resolved.structs[step.into])
				# Named after the binding, not the member: two key parts
				# through one nested struct (`h.msg`, `h.sender`) declared
				# `held_h` twice and the header did not compile.
				local = f"held_{c_name(step.member)}{step.target}"
				lines += [
					f"\t\t::situ::{held} {local};",
					f"\t\tif ({source}.{c_name(step.member)}({local}) != "
					"::situ::rt::err::ok) {",
					"\t\t\treturn ::situ::rt::err::bounds;",
					"\t\t}",
				]
				source = local
	lines.append("\t\t(void)at;")
	return lines


def _key_equal(total: int) -> str:
	"""`slot.key == key`, spelled for a byte array without <cstring>."""
	return (f"[&]() {{ for (std::uint32_t b = 0u; b < {total}u; b++) "
	        "{ if (slots_[i].key[b] != key[b]) return false; } "
	        "return true; }()")


def key_function(request: Side, layout: KeyLayout) -> list[str]:
	"""`key(...)`: the key a request with these values would have, packed
	as `record_from` packs one it reads (fuzznet, inbox 20261121T0600Z).
	C's `_key`; a part is named by its path below the request. An
	exact-bytes key is written to `out`, `total_bytes` long."""
	def part(chain: list[KeyStep]) -> str:
		return c_name(chain[-1].path.split(".", 1)[1])
	if layout.packed:
		joined = ", ".join(f"std::uint64_t {part(chain)}" for chain, _ in request)
		body = ["\t\tstd::uint64_t key = 0u;"]
		for chain, width in request:
			cut = (part(chain) if width >= 64
			       else f"({part(chain)} & UINT64_C({hex((1 << width) - 1)}))")
			body.append(f"\t\tkey = (key << {width}u) | {cut};")
		return ["\t/** The key a request with these values would have. */",
		        f"\t[[nodiscard]] static std::uint64_t key({joined}) noexcept",
		        "\t{", *body, "\t\treturn key;", "\t}", ""]
	params = []
	body = ["\t\tstd::uint32_t at = 0u;"]
	for chain, width in request:
		if isinstance(chain[-1], ReadBytes):
			params.append(f"const std::uint8_t *{part(chain)}")
			body += [f"\t\tfor (std::uint32_t b = 0u; b < {width // 8}u; b++) {{",
			         f"\t\t\tout[at + b] = {part(chain)}[b];", "\t\t}",
			         f"\t\tat += {width // 8}u;"]
		else:
			count = (width + 7) // 8
			params.append(f"std::uint64_t {part(chain)}")
			body += [f"\t\tfor (std::uint32_t b = 0u; b < {count}u; b++) {{",
			         f"\t\t\tout[at + b] = static_cast<std::uint8_t>("
			         f"{part(chain)} >> (8u * ({count}u - 1u - b)));",
			         "\t\t}", f"\t\tat += {count}u;"]
	params.append("std::uint8_t *out")
	return ["\t/** The key a request with these values would have, written",
	        f"\t *  to `out`, {layout.total_bytes} bytes. */",
	        f"\tstatic void key({', '.join(params)}) noexcept",
	        "\t{", *body, "\t\t(void)at;", "\t}", ""]


def reply_type(relation: ast.Relation, resolved: ResolvedSchema) -> str:
	"""What the reply side takes, under `::situ::`: its view, or with
	`[after]` that sealed region's gate, which only a verified open hands
	out (26.1122)."""
	region = after_region(relation)
	held = class_name(resolved.structs[relation.params[1].type_name])
	return f"{held}::{c_name(region)}_gate" if region else held


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

	guard = f"SITU_{c_name(basename).upper()}_CONVERSE_HPP"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
		" *",
		" * Conversations: a pending table per relation. The schema states",
		" * the pairing and this holds the set. A separate header -- a rung",
		" * adds files rather than changing them (decision 0032).",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <cstdint>",
		"",
		f"#include \"{basename}.hpp\"",
		"",
		"namespace situ {",
		"",
	]

	for relation, layout in ready:
		request, response = layout.request, layout.response
		name  = f"{c_name(relation.name)}_table"
		first, second = relation.params
		req   = class_name(resolved.structs[first.type_name])
		resp  = reply_type(relation, resolved)
		region = after_region(relation)
		gated = second.type_name if region else None
		asked = (_key(request, first.name, resolved) if layout.packed else
		         _key_bytes(request, first.name, resolved, layout.total_bytes))
		heard = (_key(response, second.name, resolved, gated)
		         if layout.packed else
		         _key_bytes(response, second.name, resolved,
		                    layout.total_bytes, gated))
		same = ("slots_[i].key == key" if layout.packed
		        else _key_equal(layout.total_bytes))
		lines += [
			f"/** Pending requests for `{relation.name}`.",
			" *",
			" * The slots are yours and so is their number: a full table",
			" * answers `err::bounds` rather than evicting an exchange you",
			" * still wanted. Nothing is allocated.",
			" *",
			" * The parameters are typed, so the two halves of an exchange",
			" * cannot be passed the wrong way round.",
			" *",
			" * `peer` is yours to number -- a session, the context that",
			" * verifies the reply -- and only a reply taken with the same one",
			" * answers (26.1122). The calls without `_from` hold peer 0.",
			*([f" * A reply is handed in as `{region}`'s gate: only a verified",
			   " * one ends an exchange."] if region else []),
			" */",
			f"class {name} {{",
			"public:",
			"\tstruct slot {",
			("\t\tstd::uint64_t key;" if layout.packed
			 else f"\t\tstd::uint8_t  key[{layout.total_bytes}u];"),
			"\t\tstd::uint32_t id;",
			"\t\tbool          live;",
			"\t\tstd::uint32_t peer;",
			"\t};",
			"",
			# `store`, not `slots`: Qt defines `slots` as an empty macro, so
			# the parameter would vanish in a Qt translation unit and
			# `slots_(slots)` become `slots_()` -- a null the constructor
			# writes through, compiling clean. See `cpp/drive.py`.
			f"\t{name}(slot *store, std::uint32_t cap) noexcept",
			"\t\t: slots_(store), cap_(cap)",
			"\t{",
			"\t\tfor (std::uint32_t i = 0u; i < cap_; i++) {",
			"\t\t\tslots_[i].live = false;",
			"\t\t}",
			"\t}",
			"",
			*key_function(request, layout),
			"\t/** `record_from` by a key from `key(...)`, no request in hand. */",
			("\t[[nodiscard]] ::situ::rt::err record_key_from(std::uint64_t key,"
			 if layout.packed else
			 "\t[[nodiscard]] ::situ::rt::err record_key_from("
			 "const std::uint8_t *key,"),
			"\t                                              std::uint32_t peer,",
			"\t                                              std::uint32_t id)"
			" noexcept",
			"\t{",
			"\t\tfor (std::uint32_t i = 0u; i < cap_; i++) {",
			"\t\t\tif (!slots_[i].live) {",
			*(["\t\t\t\tslots_[i] = slot{key, id, true, peer};"] if layout.packed else [
			  f"\t\t\t\tfor (std::uint32_t b = 0u; b < {layout.total_bytes}u; b++) {{",
			  "\t\t\t\t\tslots_[i].key[b] = key[b];",
			  "\t\t\t\t}",
			  "\t\t\t\tslots_[i].id   = id;",
			  "\t\t\t\tslots_[i].peer = peer;",
			  "\t\t\t\tslots_[i].live = true;"]),
			"\t\t\t\treturn ::situ::rt::err::ok;",
			"\t\t\t}",
			"\t\t}",
			"\t\treturn ::situ::rt::err::bounds;",
			"\t}",
			"",
			f"\t[[nodiscard]] ::situ::rt::err record_from(const ::situ::{req} "
			f"&{first.name},",
			"\t                                          std::uint32_t peer,",
			"\t                                          std::uint32_t id) noexcept",
			"\t{",
			*asked,
			"\t\treturn record_key_from(key, peer, id);",
			"\t}",
			"",
			f"\t[[nodiscard]] ::situ::rt::err record(const ::situ::{req} "
			f"&{first.name},",
			"\t                                     std::uint32_t id) noexcept",
			"\t{",
			f"\t\treturn record_from({first.name}, 0u, id);",
			"\t}",
			"",
			f"\t[[nodiscard]] ::situ::rt::err take_from(const ::situ::{resp} "
			f"&{second.name},",
			"\t                                        std::uint32_t peer,",
			"\t                                        std::uint32_t &id) noexcept",
			"\t{",
			*heard,
			"\t\treturn take_key_from(key, peer, id);",
			"\t}",
			"",
			"\t/** `take_from` by a key from `key(...)`: for a reply verified",
			"\t *  and reassembled before it is matched, whose key's values the",
			"\t *  caller holds by then rather than the frame (fuzznet, inbox",
			"\t *  20261121T1530Z). */",
			("\t[[nodiscard]] ::situ::rt::err take_key_from(std::uint64_t key,"
			 if layout.packed else
			 "\t[[nodiscard]] ::situ::rt::err take_key_from("
			 "const std::uint8_t *key,"),
			"\t                                            std::uint32_t peer,",
			"\t                                            std::uint32_t &id)"
			" noexcept",
			"\t{",
			"\t\tfor (std::uint32_t i = 0u; i < cap_; i++) {",
			f"\t\t\tif (slots_[i].live && slots_[i].peer == peer && {same}) {{",
			"\t\t\t\tid = slots_[i].id;",
			"\t\t\t\tslots_[i].live = false;",
			"\t\t\t\treturn ::situ::rt::err::ok;",
			"\t\t\t}",
			"\t\t}",
			"\t\treturn ::situ::rt::err::constraint;",
			"\t}",
			"",
			f"\t[[nodiscard]] ::situ::rt::err take(const ::situ::{resp} "
			f"&{second.name},",
			"\t                                   std::uint32_t &id) noexcept",
			"\t{",
			f"\t\treturn take_from({second.name}, 0u, id);",
			"\t}",
			"",
			"\t/** `take` without taking: which request a reply answers, left",
			"\t *  pending for a reply that comes in pieces (26.1009). */",
			f"\t[[nodiscard]] ::situ::rt::err peek_from(const ::situ::{resp} "
			f"&{second.name},",
			"\t                                        std::uint32_t peer,",
			"\t                                        std::uint32_t &id) const noexcept",
			"\t{",
			*heard,
			"",
			"\t\tfor (std::uint32_t i = 0u; i < cap_; i++) {",
			f"\t\t\tif (slots_[i].live && slots_[i].peer == peer && {same}) {{",
			"\t\t\t\tid = slots_[i].id;",
			"\t\t\t\treturn ::situ::rt::err::ok;",
			"\t\t\t}",
			"\t\t}",
			"\t\treturn ::situ::rt::err::constraint;",
			"\t}",
			"",
			f"\t[[nodiscard]] ::situ::rt::err peek(const ::situ::{resp} "
			f"&{second.name},",
			"\t                                   std::uint32_t &id) const noexcept",
			"\t{",
			f"\t\treturn peek_from({second.name}, 0u, id);",
			"\t}",
			"",
			"private:",
			"\tslot          *slots_;",
			"\tstd::uint32_t  cap_;",
			"};",
			"",
		]

	lines += ["}  /* namespace situ */", "", f"#endif /* {guard} */"]
	return {f"{basename}_converse.hpp": "\n".join(lines) + "\n"}
