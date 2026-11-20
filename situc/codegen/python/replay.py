"""A replay window in Python: `<basename>_replay.py` (26.1120).

The C backend's two forms and two calls, the same verdicts numbered the
same way: a pure `check` asked before the tag and `record` after it. The
state is the caller's object; a counter window per session, a nonce set of
`CAPACITY` slots. See `situc/codegen/c/replay.py` for why each rule is
there; this says it again in Python and decides nothing of its own.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen.c.replay import (VERDICTS, _attr, _number, _path, _word,
                                    refusals)
from situc.resolve import ResolvedSchema


def _class(name: str) -> str:
	return "".join(part.capitalize() for part in name.split("_"))


def _read(path: str) -> str:
	"""`m.head.seq` reads as written: a nested member is a view."""
	return path


def _counter(replay: ast.Replay) -> list[str]:
	window = _number(replay, "window")
	param  = replay.param.name
	value  = _read(_path(replay, "counter"))
	return [
		f"class {_class(replay.name)}:",
		f'\t"""`{replay.name}`: the last {window} counters of',
		f"\t`{_path(replay, 'counter')}` seen, below the highest. One session's,",
		"\tthe caller deciding what a session is. A counter that wrapped reads",
		'\tas older than the window and is refused, never as new."""',
		"",
		f"\tWINDOW = {window}",
		"",
		"\tdef __init__(self) -> None:",
		"\t\tself.top = 0",
		"\t\tself.seen = 0",
		"\t\tself.any = False",
		"",
		f"\tdef check(self, {param}) -> int:",
		'\t\t"""Whether the counter may be accepted. Pure: ask it BEFORE the',
		'\t\ttag, so a replay costs no AEAD work."""',
		f"\t\tcounter = int({value})",
		"\t\tif not self.any or counter > self.top:",
		"\t\t\treturn OK",
		"\t\tif self.top - counter >= self.WINDOW:",
		"\t\t\treturn TOO_OLD",
		"\t\tif (self.seen >> (self.top - counter)) & 1:",
		"\t\t\treturn REPLAY",
		"\t\treturn OK",
		"",
		f"\tdef record(self, {param}) -> int:",
		'\t\t"""`check`, and the counter recorded where it says OK. Ask it',
		'\t\tAFTER the tag verifies, so a forgery records nothing."""',
		f"\t\tverdict = self.check({param})",
		"\t\tif verdict != OK:",
		"\t\t\treturn verdict",
		f"\t\tcounter = int({value})",
		"\t\tif not self.any:",
		"\t\t\tself.any, self.top, self.seen = True, counter, 1",
		"\t\telif counter > self.top:",
		"\t\t\tup = counter - self.top",
		"\t\t\tself.seen = 1 if up >= 64 else "
		"((self.seen << up) | 1) & ((1 << 64) - 1)",
		"\t\t\tself.top = counter",
		"\t\telse:",
		"\t\t\tself.seen |= 1 << (self.top - counter)",
		"\t\treturn OK",
		"",
		"",
	]


def _nonce(replay: ast.Replay) -> list[str]:
	capacity = _number(replay, "capacity")
	ahead    = _number(replay, "ahead")
	unit     = _word(replay, "unit", "ms")
	required = _word(replay, "expiry", "required") == "required"
	param    = replay.param.name
	nonce    = _read(_path(replay, "nonce"))
	expires  = _read(_path(replay, "expires"))
	zero = ("\t\t\treturn NO_EXPIRY" if required else
	        "\t\t\treturn OK\t# none stated: allowed, and nothing remembered")
	return [
		f"class {_class(replay.name)}:",
		f'\t"""`{replay.name}`: each `{_path(replay, "nonce")}` accepted once until',
		f"\tits `{_path(replay, 'expires')}`, in {unit}, no further ahead of `now`",
		f"\tthan {ahead} {unit}. {capacity} slots; FULL refuses and never evicts,",
		"\tsince evicting re-admits the evicted nonce when it is resent. Size",
		"\tit as capacity >= peak admitted rate x ahead, admitted meaning after",
		'\tthe tag (26.1120)."""',
		"",
		f"\tCAPACITY = {capacity}",
		f"\tAHEAD = {ahead}\t# {unit}",
		"",
		"\tdef __init__(self) -> None:",
		"\t\tself.slots = [(0, 0)] * self.CAPACITY\t# (nonce, expires)",
		"",
		f"\tdef check(self, {param}, now: int) -> int:",
		f'\t\t"""Whether the nonce may be accepted at `now` ({unit}). Pure: ask',
		'\t\tit BEFORE the tag."""',
		f"\t\tnonce, expires = int({nonce}), int({expires})",
		"\t\tif expires == 0:",
		zero,
		"\t\tif expires <= now:",
		"\t\t\treturn EXPIRED",
		"\t\tif expires - now > self.AHEAD:",
		"\t\t\treturn HORIZON",
		"\t\tfree = False",
		"\t\tfor held, until in self.slots:",
		"\t\t\tif until <= now:",
		"\t\t\t\tfree = True",
		"\t\t\telif held == nonce:",
		"\t\t\t\treturn REPLAY",
		"\t\treturn OK if free else FULL",
		"",
		f"\tdef record(self, {param}, now: int) -> int:",
		'\t\t"""`check`, and the nonce recorded where it says OK and an expiry',
		'\t\tis stated. Ask it AFTER the tag verifies."""',
		f"\t\tverdict = self.check({param}, now)",
		"\t\tif verdict != OK:",
		"\t\t\treturn verdict",
		f"\t\tnonce, expires = int({nonce}), int({expires})",
		"\t\tif expires == 0:",
		"\t\t\treturn OK",
		"\t\tfor at, (_, until) in enumerate(self.slots):",
		"\t\t\tif until <= now:",
		"\t\t\t\tself.slots[at] = (nonce, expires)",
		"\t\t\t\tbreak",
		"\t\treturn OK",
		"",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	refused = {name for name, _ in refusals(schema, resolved)}
	replays = [one for one in schema.replays() if one.name not in refused]
	if not replays:
		return {}
	lines = [
		f'"""Generated by situc {__version__} from {basename}.situ -- do not edit.',
		"",
		"Replay windows (26.1120): a pure `check` asked before the tag and",
		'`record` after it. The state is the caller\'s."""',
		"",
		*(f"{one.upper()} = {at}" for at, one in enumerate(VERDICTS)),
		"",
		"",
	]
	for replay in replays:
		lines += (_counter(replay) if _attr(replay, "counter") is not None
		          else _nonce(replay))
	return {f"{basename}_replay.py": "\n".join(lines).rstrip("\n") + "\n"}
