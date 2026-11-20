"""A replay window in Rust: `<basename>_replay.rs` (26.1120).

The C backend's two forms and two calls -- a pure `check` before the tag,
`record` after it -- with the same verdicts numbered the same way, reading
the fields through the Rust views. See `situc/codegen/c/replay.py` for why
each rule is there.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.replay import (VERDICTS, _attr, _number, _path, _word,
                                    refusals)
from situc.relation import Read, SubView, path_bindings
from situc.resolve import ResolvedSchema


def _pascal(name: str) -> str:
	return "".join(part.capitalize() for part in name.split("_"))


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
				lines.append(f"\t\tlet {target} = {source}.{bind.member}()?;"
				             f"\t// {bind.path}")
			else:
				assert isinstance(bind, Read)
				lines.append(f"\t\tlet {target} = u64::from("
				             f"{source}.{bind.member}());\t// {bind.path}")
		names[key] = f"situ_v{bindings[-1].target}"
	return lines, names


def _counter(replay: ast.Replay, resolved: ResolvedSchema) -> list[str]:
	window = _number(replay, "window")
	param  = replay.param.name
	kind   = _pascal(replay.param.type_name)
	reads, local = _reads(replay, resolved, ("counter",))
	c = local["counter"]
	return [
		f"/// `{replay.name}`: the last {window} counters of "
		f"`{_path(replay, 'counter')}` seen, below",
		"/// the highest -- one session's, the caller deciding what a session",
		"/// is. A wrapped counter reads as older than the window and is",
		"/// refused, never as new.",
		"#[derive(Debug, Default)]",
		f"pub struct {_pascal(replay.name)} {{",
		"\tpub top: u64,",
		"\tpub seen: u64,",
		"\tpub any: bool,",
		"}",
		"",
		f"impl {_pascal(replay.name)} {{",
		f"\tpub const WINDOW: u64 = {window};",
		"",
		"\t/// Pure: ask it BEFORE the tag.",
		f"\tpub fn check(&self, {param}: &{kind}<'_>) -> Result<Verdict> {{",
		*reads,
		f"\t\tif !self.any || {c} > self.top {{",
		"\t\t\treturn Ok(Verdict::Ok);",
		"\t\t}",
		f"\t\tif self.top - {c} >= Self::WINDOW {{",
		"\t\t\treturn Ok(Verdict::TooOld);",
		"\t\t}",
		f"\t\tif (self.seen >> (self.top - {c})) & 1 != 0 {{",
		"\t\t\treturn Ok(Verdict::Replay);",
		"\t\t}",
		"\t\tOk(Verdict::Ok)",
		"\t}",
		"",
		"\t/// `check`, and the counter recorded on OK: AFTER the tag.",
		f"\tpub fn record(&mut self, {param}: &{kind}<'_>) -> Result<Verdict> {{",
		f"\t\tlet verdict = self.check({param})?;",
		"\t\tif verdict != Verdict::Ok {",
		"\t\t\treturn Ok(verdict);",
		"\t\t}",
		*reads,
		"\t\tif !self.any {",
		f"\t\t\tself.any = true;",
		f"\t\t\tself.top = {c};",
		"\t\t\tself.seen = 1;",
		f"\t\t}} else if {c} > self.top {{",
		f"\t\t\tlet up = {c} - self.top;",
		"\t\t\tself.seen = if up >= 64 { 1 } else { (self.seen << up) | 1 };",
		f"\t\t\tself.top = {c};",
		"\t\t} else {",
		f"\t\t\tself.seen |= 1u64 << (self.top - {c});",
		"\t\t}",
		"\t\tOk(Verdict::Ok)",
		"\t}",
		"}",
		"",
	]


def _nonce(replay: ast.Replay, resolved: ResolvedSchema) -> list[str]:
	least    = (_number(replay, "capacity")
	            if _attr(replay, "capacity") is not None else 1)
	ahead    = _number(replay, "ahead")
	unit     = _word(replay, "unit", "ms")
	required = _word(replay, "expiry", "required") == "required"
	param    = replay.param.name
	kind     = _pascal(replay.param.type_name)
	reads, local = _reads(replay, resolved, ("nonce", "expires"))
	nonce, expires = local["nonce"], local["expires"]
	zero = ("\t\t\treturn Ok(Verdict::NoExpiry);" if required else
	        "\t\t\treturn Ok(Verdict::Ok); // none stated: allowed")
	name = _pascal(replay.name)
	return [
		f"/// `{replay.name}`: each `{_path(replay, 'nonce')}` accepted once"
		f" until `{_path(replay, 'expires')}`,",
		f"/// in {unit}, no further ahead than {ahead}. The table is the caller's:",
		"/// count >= peak admitted rate x ahead. FULL refuses, never evicts.",
		"#[derive(Debug)]",
		f"pub struct {name}<'a> {{",
		"\tpub slots: &'a mut [(u64, u64)],\t// (nonce, expires), zeroed",
		"}",
		"",
		f"impl<'a> {name}<'a> {{",
		f"\tpub const LEAST: usize = {least};",
		f"\tpub const AHEAD: u64 = {ahead}; // {unit}",
		"",
		"\t/// Pure: ask it BEFORE the tag.",
		f"\tpub fn check(&self, {param}: &{kind}<'_>, now: u64)"
		" -> Result<Verdict> {",
		"\t\tif self.slots.len() < Self::LEAST {",
		"\t\t\treturn Err(Error::Bounds);",
		"\t\t}",
		*reads,
		f"\t\tif {expires} == 0 {{",
		zero,
		"\t\t}",
		f"\t\tif {expires} <= now {{",
		"\t\t\treturn Ok(Verdict::Expired);",
		"\t\t}",
		f"\t\tif {expires} - now > Self::AHEAD {{",
		"\t\t\treturn Ok(Verdict::Horizon);",
		"\t\t}",
		"\t\tlet mut free = false;",
		"\t\tfor &(held, until) in self.slots.iter() {",
		"\t\t\tif until <= now {",
		"\t\t\t\tfree = true;",
		f"\t\t\t}} else if held == {nonce} {{",
		"\t\t\t\treturn Ok(Verdict::Replay);",
		"\t\t\t}",
		"\t\t}",
		"\t\tOk(if free { Verdict::Ok } else { Verdict::Full })",
		"\t}",
		"",
		"\t/// `check`, and the nonce recorded on OK: AFTER the tag.",
		f"\tpub fn record(&mut self, {param}: &{kind}<'_>, now: u64)"
		" -> Result<Verdict> {",
		f"\t\tlet verdict = self.check({param}, now)?;",
		"\t\tif verdict != Verdict::Ok {",
		"\t\t\treturn Ok(verdict);",
		"\t\t}",
		*reads,
		f"\t\tif {expires} == 0 {{",
		"\t\t\treturn Ok(Verdict::Ok);",
		"\t\t}",
		"\t\tfor slot in self.slots.iter_mut() {",
		"\t\t\tif slot.1 <= now {",
		f"\t\t\t\t*slot = ({nonce}, {expires});",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t}",
		"\t\tOk(Verdict::Ok)",
		"\t}",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	replays = [one for one in schema.replays() if one.name not in refused]
	if not replays:
		return {}
	lines = [
		f"// Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		"//",
		"// Replay windows (26.1120): a pure `check` asked before the tag and",
		"// `record` after it. The state is the caller's.",
		"",
		f"use crate::{basename}::*;",
		"use crate::situ_rt::{Error, Result};",
		"",
		"#[derive(Debug, Clone, Copy, PartialEq, Eq)]",
		"pub enum Verdict {",
		*(f"\t{_pascal(one)} = {at}," for at, one in enumerate(VERDICTS)),
		"}",
		"",
	]
	for replay in replays:
		lines += (_counter(replay, resolved) if _attr(replay, "counter")
		          is not None else _nonce(replay, resolved))
	return {f"{basename}_replay.rs": "\n".join(lines)}
