"""A replay window in C++: `<basename>_replay.hpp` (26.1120).

The C backend's two forms and two calls -- a pure `check` before the tag,
`record` after it -- with the same verdicts numbered the same way, reading
the fields through the C++ views. See `situc/codegen/c/replay.py` for why
each rule is there.
"""

from __future__ import annotations

from situc.timespec import clock_unit
from situc import __version__, ast
from situc.codegen.c.replay import (VERDICTS, _attr, _number, _path, _word,
                                    refusals)
from situc.relation import Read, SubView, path_bindings
from situc.resolve import ResolvedSchema


def _reads(replay: ast.Replay, resolved: ResolvedSchema,
		keys: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
	lines: list[str] = []
	names: dict[str, str] = {}
	first = 0
	for key in keys:
		bindings, _ = path_bindings(_path(replay, key), replay.param,
		                            resolved, first)
		first += len(bindings)
		for bind in bindings:
			source = bind.source if not bind.source.startswith("_") \
				else f"situ_v{bind.source}"
			target = f"situ_v{bind.target}"
			if isinstance(bind, SubView):
				lines += [
					f"\t::situ::{bind.into} {target};\t// {bind.path}",
					f"\tif (const auto e = {source}.{bind.member}({target});"
					" e != ::situ::rt::err::ok) {",
					"\t\treturn e;",
					"\t}",
				]
			else:
				assert isinstance(bind, Read)
				lines.append(f"\tconst std::uint64_t {target} = "
				             f"{source}.{bind.member}();\t// {bind.path}")
		names[key] = f"situ_v{bindings[-1].target}"
	return lines, names


def _counter(replay: ast.Replay, resolved: ResolvedSchema) -> list[str]:
	window = _number(replay, "window")
	param  = replay.param.name
	view   = f"const ::situ::{replay.param.type_name} &{param}"
	reads, local = _reads(replay, resolved, ("counter",))
	c = local["counter"]
	return [
		f"/** `{replay.name}`: the last {window} counters of "
		f"`{_path(replay, 'counter')}` seen,",
		" *  below the highest -- one session's, the caller deciding what a",
		" *  session is. A wrapped counter reads as older than the window",
		" *  and is refused, never as new. */",
		f"struct {replay.name} {{",
		f"\tstatic constexpr std::uint64_t window = {window};",
		"\tstd::uint64_t top = 0;",
		"\tstd::uint64_t seen = 0;",
		"\tbool any = false;",
		"",
		"\t/** Pure: ask it BEFORE the tag. */",
		f"\t[[nodiscard]] ::situ::rt::err check({view},",
		"\t\t\tverdict &out) const noexcept",
		"\t{",
		*(f"\t{line}" for line in reads),
		"\t\tout = verdict::ok;",
		f"\t\tif (!any || {c} > top) {{",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		f"\t\tif (top - {c} >= window) {{",
		"\t\t\tout = verdict::too_old;",
		f"\t\t}} else if ((seen >> (top - {c})) & 1u) {{",
		"\t\t\tout = verdict::replay;",
		"\t\t}",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"",
		"\t/** `check`, and the counter recorded on OK: AFTER the tag. */",
		f"\t[[nodiscard]] ::situ::rt::err record({view},",
		"\t\t\tverdict &out) noexcept",
		"\t{",
		f"\t\tif (const auto e = check({param}, out);"
		" e != ::situ::rt::err::ok || out != verdict::ok) {",
		"\t\t\treturn e;",
		"\t\t}",
		*(f"\t{line}" for line in reads),
		"\t\tif (!any) {",
		f"\t\t\tany = true; top = {c}; seen = 1u;",
		f"\t\t}} else if ({c} > top) {{",
		f"\t\t\tconst std::uint64_t up = {c} - top;",
		"\t\t\tseen = up >= 64u ? 1u : (seen << up) | 1u;",
		f"\t\t\ttop = {c};",
		"\t\t} else {",
		f"\t\t\tseen |= std::uint64_t{{1}} << (top - {c});",
		"\t\t}",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"};",
		"",
	]


def _nonce(replay: ast.Replay, resolved: ResolvedSchema,
           unit: str) -> list[str]:
	least    = (_number(replay, "capacity")
	            if _attr(replay, "capacity") is not None else 1)
	ahead    = _number(replay, "ahead")
	rule     = _word(replay, "expiry", "required")
	required = rule == "required"
	given    = ", bool expiry_required" if rule == "caller" else ""
	passed   = ", expiry_required" if rule == "caller" else ""
	param    = replay.param.name
	view     = f"const ::situ::{replay.param.type_name} &{param}"
	reads, local = _reads(replay, resolved, ("nonce", "expires"))
	nonce, expires = local["nonce"], local["expires"]
	zero = ("\t\t\tout = verdict::no_expiry;" if required else
	        "\t\t\tif (expiry_required) out = verdict::no_expiry;"
	        if rule == "caller" else
	        "\t\t\t// none stated: allowed, and nothing remembered")
	return [
		f"/** `{replay.name}`: each `{_path(replay, 'nonce')}` accepted once"
		f" until `{_path(replay, 'expires')}`,",
		f" *  in {unit}, no further ahead than {ahead}. The table is the"
		" caller's:",
		" *  count >= peak admitted rate x ahead. FULL refuses, never evicts. */",
		f"struct {replay.name} {{",
		f"\tstatic constexpr std::uint32_t least = {least};",
		f"\tstatic constexpr std::uint64_t ahead = {ahead};"
		f"\t// {unit}, the most a window allows",
		"\tstruct slot { std::uint64_t nonce = 0, expires = 0; };",
		"\tslot *slots = nullptr;",
		"\tstd::uint32_t count = 0;",
		f"\tstd::uint64_t max_ahead = 0;\t// {unit}; 0 is `ahead`, more is refused",
		"",
		"\t/** Pure: ask it BEFORE the tag. */",
		f"\t[[nodiscard]] ::situ::rt::err check({view},",
		f"\t\t\tstd::uint64_t now{given}, verdict &out) const noexcept",
		"\t{",
		*(f"\t{line}" for line in reads),
		"\t\tbool free_slot = false;",
		"",
		"\t\tif (slots == nullptr || count < least || max_ahead > ahead) {",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		"\t\tout = verdict::ok;",
		f"\t\tif ({expires} == 0u) {{",
		zero,
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		f"\t\tif ({expires} <= now) {{",
		"\t\t\tout = verdict::expired;",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		f"\t\tif ({expires} - now > (max_ahead != 0u ? max_ahead : ahead)) {{",
		"\t\t\tout = verdict::horizon;",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		"\t\tfor (std::uint32_t i = 0; i < count; i++) {",
		"\t\t\tconst slot &held = slots[i];",
		"\t\t\tif (held.expires <= now) {",
		"\t\t\t\tfree_slot = true;",
		f"\t\t\t}} else if (held.nonce == {nonce}) {{",
		"\t\t\t\tout = verdict::replay;",
		"\t\t\t\treturn ::situ::rt::err::ok;",
		"\t\t\t}",
		"\t\t}",
		"\t\tif (!free_slot) {",
		"\t\t\tout = verdict::full;",
		"\t\t}",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"",
		"\t/** `check`, and the nonce recorded on OK: AFTER the tag. */",
		f"\t[[nodiscard]] ::situ::rt::err record({view},",
		f"\t\t\tstd::uint64_t now{given}, verdict &out) noexcept",
		"\t{",
		f"\t\tif (const auto e = check({param}, now{passed}, out);"
		" e != ::situ::rt::err::ok || out != verdict::ok) {",
		"\t\t\treturn e;",
		"\t\t}",
		*(f"\t{line}" for line in reads),
		f"\t\tif ({expires} == 0u) {{",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		"\t\tfor (std::uint32_t i = 0; i < count; i++) {",
		"\t\t\tslot &held = slots[i];",
		"\t\t\tif (held.expires <= now) {",
		f"\t\t\t\theld.nonce = {nonce};",
		f"\t\t\t\theld.expires = {expires};",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"};",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	replays = [one for one in schema.replays() if one.name not in refused]
	if not replays:
		return {}
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * Replay windows (26.1120): a pure `check` asked before the tag and",
		" * `record` after it. The state is the caller's. */",
		"#pragma once",
		"",
		f"#include \"{basename}.hpp\"",
		"",
		"namespace situ_replay {",
		"",
		"enum class verdict : std::uint8_t {",
		*(f"\t{one} = {at}," for at, one in enumerate(VERDICTS)),
		"};",
		"",
	]
	for replay in replays:
		lines += (_counter(replay, resolved) if _attr(replay, "counter")
		          is not None else _nonce(replay, resolved,
		                                    clock_unit(schema, replay)))
	lines += ["}  // namespace situ_replay", ""]
	return {f"{basename}_replay.hpp": "\n".join(lines)}
