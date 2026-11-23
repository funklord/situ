"""A replay window in C: `<basename>_replay.h` (26.1120).

Which messages a receiver accepts once, in the two forms fuzznet's own code
uses: a COUNTER window -- the last `window` counters, DTLS's and IPsec's
bitmap -- and a NONCE set with expiries, `capacity` slots of the caller's
memory, nothing further ahead than `ahead`. Two calls, because fuzznet's
review of the design showed the one `admit` first drafted loses a property:
`check` is pure and asked BEFORE the tag, so a replay costs no AEAD work,
and `record` is asked AFTER it, so a forgery records nothing.

What `check` answers is a verdict beside the read's own error: a message
whose counter cannot be read is BOUNDS like any accessor's, and one that
reads and has been seen is a verdict, not a malformed message.
"""

from __future__ import annotations

from situc.timespec import clock_unit
from situc import __version__, ast
from situc.codegen.c.names import ident, macro, header_guard
from situc.codegen.c.relate import _binding, _local
from situc.relation import ReadBytes, Refused, path_bindings, paths_in
from situc.resolve import ResolvedSchema

#: The verdicts, in the order every backend numbers them.
VERDICTS = ("ok", "replay", "too_old", "expired", "horizon", "full",
            "no_expiry")


def _attr(replay: ast.Replay, name: str) -> ast.Attr | None:
	return next((attr for attr in replay.attrs if attr.name == name), None)


def _number(replay: ast.Replay, name: str) -> int:
	held = _attr(replay, name)
	value = getattr(held.value, "value", None) if held is not None else None
	assert isinstance(value, int), (replay.name, name)
	return value


def _word(replay: ast.Replay, name: str, default: str) -> str:
	held = _attr(replay, name)
	value = getattr(held.value, "name", None) if held is not None else None
	return value if isinstance(value, str) else default


def _path(replay: ast.Replay, name: str) -> str:
	held = _attr(replay, name)
	assert held is not None and held.value is not None, (replay.name, name)
	return paths_in(held.value)[0]


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""A window whose fields this backend cannot read, by name."""
	found: list[tuple[str, str]] = []
	for replay in schema.replays():
		keys = (("counter",) if _attr(replay, "counter") is not None
		        else ("nonce", "expires"))
		for key in keys:
			try:
				path_bindings(_path(replay, key), replay.param, resolved,
				              bytes_ok=key == "nonce")
			except Refused as why:
				found.append((replay.name, str(why)))
				break
	return found


def _reads(replay: ast.Replay, resolved: ResolvedSchema, keys: tuple[str, ...],
		prefix: str) -> tuple[list[str], dict[str, str]]:
	"""Statements reading each field into a `uint64_t` -- or, for a nonce
	that is a byte run, a pointer to its bytes -- and its local."""
	lines: list[str] = []
	names: dict[str, str] = {}
	first = 0
	for key in keys:
		bindings, _ = path_bindings(_path(replay, key), replay.param,
		                            resolved, first, bytes_ok=key == "nonce")
		first += len(bindings)
		for bind in bindings:
			lines += _binding(bind, "uint64_t", prefix)
		names[key] = _local(bindings[-1].target)
	return lines, names


def _counter(replay: ast.Replay, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	name    = ident(prefix, replay.name)
	upper   = macro(prefix, replay.name)
	window  = _number(replay, "window")
	verdict = ident(prefix, "replay", "verdict")
	reads, local = _reads(replay, resolved, ("counter",), prefix)
	counter = local["counter"]
	param   = replay.param.name
	return [
		f"/* `{replay.name}`: the last {window} counters of "
		f"`{_path(replay, 'counter')}` seen, as a",
		" * bitmap below the highest -- the state of one session, handed over",
		" * by the caller, which decides what a session is (26.1120). A counter",
		" * that wrapped reads as older than the window and is refused, never",
		" * as new. Zero-initialise it before the first message. Its three",
		" * fields are stable: a caller whose own public struct holds the",
		" * window may keep them there and build this from them. */",
		f"#define {upper}_WINDOW {window}u",
		"typedef struct {",
		"\tuint64_t top;\t/* the highest counter recorded */",
		"\tuint64_t seen;\t/* bit d: counter top - d recorded */",
		"\tuint8_t  any;\t/* whether anything is recorded yet */",
		f"}} {name}_t;",
		"",
		f"/** Whether the message's counter may be accepted. Pure: ask it",
		" *  BEFORE the tag, so a replay costs no AEAD work. */",
		f"static inline situ_err_t {name}_check(const {name}_t *state,",
		f"\t\tsitu_view_t {param}, {verdict} *verdict)",
		"{",
		"\tsitu_err_t err = SITU_OK;",
		"",
		*reads,
		"\t(void)err;",
		f"\t*verdict = {macro(prefix, 'replay', 'ok')};",
		f"\tif (!state->any || {counter} > state->top) {{",
		"\t\treturn SITU_OK;",
		"\t}",
		f"\tif (state->top - {counter} >= {upper}_WINDOW) {{",
		f"\t\t*verdict = {macro(prefix, 'replay', 'too_old')};",
		f"\t}} else if ((state->seen >> (state->top - {counter})) & 1u) {{",
		f"\t\t*verdict = {macro(prefix, 'replay', 'replay')};",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		f"/** `{name}_check`, and the counter recorded where it says OK. Ask",
		" *  it AFTER the tag verifies, so a forgery records nothing. */",
		f"static inline situ_err_t {name}_record({name}_t *state,",
		f"\t\tsitu_view_t {param}, {verdict} *verdict)",
		"{",
		f"\tsitu_err_t err = {name}_check(state, {param}, verdict);",
		"",
		f"\tif (err != SITU_OK || *verdict != {macro(prefix, 'replay', 'ok')}) {{",
		"\t\treturn err;",
		"\t}",
		*reads,
		f"\tif (!state->any) {{",
		"\t\tstate->any  = 1u;",
		f"\t\tstate->top  = {counter};",
		"\t\tstate->seen = 1u;",
		f"\t}} else if ({counter} > state->top) {{",
		f"\t\tconst uint64_t up = {counter} - state->top;",
		"",
		"\t\tstate->seen = up >= 64u ? 1u : (state->seen << up) | 1u;",
		f"\t\tstate->top  = {counter};",
		"\t} else {",
		f"\t\tstate->seen |= (uint64_t)1u << (state->top - {counter});",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
	]


def nonce_bytes(replay: ast.Replay, resolved: ResolvedSchema) -> int | None:
	"""How many bytes a nonce that is a byte run holds, or None for a
	scalar one (fuzznet, inbox 20261123T1215Z: XChaCha's 24 random
	bytes, compared by their bytes)."""
	bindings, _ = path_bindings(_path(replay, "nonce"), replay.param,
	                            resolved, bytes_ok=True)
	last = bindings[-1]
	return last.length if isinstance(last, ReadBytes) else None


def _nonce(replay: ast.Replay, resolved: ResolvedSchema, unit: str,
		prefix: str) -> list[str]:
	name     = ident(prefix, replay.name)
	upper    = macro(prefix, replay.name)
	least    = (_number(replay, "capacity")
	            if _attr(replay, "capacity") is not None else 1)
	ahead    = _number(replay, "ahead")
	rule     = _word(replay, "expiry", "required")
	required = rule == "required"
	# `expiry = caller`: the rule is the consumer's, per message, at run
	# time -- fuzznet's kinds are its consumers' vocabulary, not the
	# schema's (inbox 20261120T1930Z) -- so the calls take it.
	given    = ", int expiry_required" if rule == "caller" else ""
	passed   = ", expiry_required" if rule == "caller" else ""
	verdict  = ident(prefix, "replay", "verdict")
	reads, local = _reads(replay, resolved, ("nonce", "expires"), prefix)
	nonce, expires = local["nonce"], local["expires"]
	width    = nonce_bytes(replay, resolved)
	held_nonce = ("\tuint64_t nonce;" if width is None
	              else f"\tuint8_t  nonce[{width}];\t/* its bytes */")
	same     = (f"state->slots[i].nonce == {nonce}" if width is None else
	            f"memcmp(state->slots[i].nonce, {nonce}, {width}u) == 0")
	keep     = (f"\t\t\tstate->slots[i].nonce   = {nonce};" if width is None
	            else f"\t\t\tmemcpy(state->slots[i].nonce, {nonce}, {width}u);")
	param    = replay.param.name
	ok       = macro(prefix, "replay", "ok")
	no_expiry = macro(prefix, 'replay', 'no_expiry')
	zero = ([f"\t\t*verdict = {no_expiry};"] if required else
	        ["\t\tif (expiry_required) {",
	         f"\t\t\t*verdict = {no_expiry};",
	         "\t\t}"] if rule == "caller" else
	        ["\t\t/* None stated, which this window allows: nothing is",
	         "\t\t * remembered for it, so there is nothing to bound. */"])
	return [
		f"/* `{replay.name}`: each `{_path(replay, 'nonce')}` accepted once"
		" until its",
		f" * `{_path(replay, 'expires')}`, in {unit}, which may be no further"
		f" ahead of `now`",
		f" * than {ahead} {unit} -- so a frame cannot pin a slot for a century.",
		" * The table is the CALLER's -- its own slots and their count, zeroed",
		" * -- because its size is the consumer's: size it as",
		" *     count >= peak admitted rate x ahead",
		" * where admitted means after the tag (fuzznet's rule, 26.1120); a",
		" * phone and a relay differ by orders. FULL refuses and never evicts:",
		" * evicting re-admits the evicted nonce when it is resent. The fields",
		" * of both types are stable, for a caller that keeps them itself. */",
		f"#define {upper}_LEAST {least}u\t/* the smallest table allowed */",
		f"#define {upper}_AHEAD {ahead}u\t/* {unit}, the most a window allows */",
		"typedef struct {",
		held_nonce,
		"\tuint64_t expires;\t/* 0, or at or before `now`: free */",
		f"}} {name}_slot_t;",
		"typedef struct {",
		f"\t{name}_slot_t *slots;",
		"\tuint32_t count;",
		# The caller's horizon, as the table is (fuzznet, inbox
		# 20261123T1215Z): a daemon and a test differ, and the schema's
		# `ahead` is the ceiling no window may pass.
		f"\tuint64_t max_ahead;\t/* {unit}; 0 is {upper}_AHEAD, more is refused */",
		f"}} {name}_t;",
		"",
		f"/** Whether the message's nonce may be accepted at `now` ({unit}).",
		" *  Pure: ask it BEFORE the tag. */",
		f"static inline situ_err_t {name}_check(const {name}_t *state,",
		f"\t\tsitu_view_t {param}, uint64_t now{given}, {verdict} *verdict)",
		"{",
		"\tsitu_err_t err = SITU_OK;",
		"\tint        free_slot = 0;",
		"",
		f"\tif (state->slots == NULL || state->count < {upper}_LEAST",
		f"\t                || state->max_ahead > {upper}_AHEAD) {{",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		*reads,
		"\t(void)err;",
		f"\t*verdict = {ok};",
		f"\tif ({expires} == 0u) {{",
		*zero,
		"\t\treturn SITU_OK;",
		"\t}",
		f"\tif ({expires} <= now) {{",
		f"\t\t*verdict = {macro(prefix, 'replay', 'expired')};",
		"\t\treturn SITU_OK;",
		"\t}",
		f"\tif ({expires} - now > (state->max_ahead != 0u"
		f" ? state->max_ahead : {upper}_AHEAD)) {{",
		f"\t\t*verdict = {macro(prefix, 'replay', 'horizon')};",
		"\t\treturn SITU_OK;",
		"\t}",
		f"\tfor (uint32_t i = 0u; i < state->count; i++) {{",
		"\t\tif (state->slots[i].expires <= now) {",
		"\t\t\tfree_slot = 1;",
		f"\t\t}} else if ({same}) {{",
		f"\t\t\t*verdict = {macro(prefix, 'replay', 'replay')};",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\tif (!free_slot) {",
		f"\t\t*verdict = {macro(prefix, 'replay', 'full')};",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		f"/** `{name}_check`, and the nonce recorded where it says OK and an",
		" *  expiry is stated. Ask it AFTER the tag verifies. */",
		f"static inline situ_err_t {name}_record({name}_t *state,",
		f"\t\tsitu_view_t {param}, uint64_t now{given}, {verdict} *verdict)",
		"{",
		f"\tsitu_err_t err = {name}_check(state, {param}, now{passed}, "
		"verdict);",
		"",
		f"\tif (err != SITU_OK || *verdict != {ok}) {{",
		"\t\treturn err;",
		"\t}",
		*reads,
		f"\tif ({expires} == 0u) {{",
		"\t\treturn SITU_OK;",
		"\t}",
		f"\tfor (uint32_t i = 0u; i < state->count; i++) {{",
		"\t\tif (state->slots[i].expires <= now) {",
		keep,
		f"\t\t\tstate->slots[i].expires = {expires};",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""`<basename>_replay.h`, or nothing where the schema states no window."""
	replays = schema.replays()
	refused = {name for name, _ in refusals(schema, resolved)}
	replays = [one for one in replays if one.name not in refused]
	if not replays:
		return {}
	guard = header_guard(prefix, basename, "replay", "h")
	verdict = ident(prefix, "replay", "verdict")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * Replay windows (26.1120): a pure `_check` asked before the tag, and",
		" * `_record` after it. The state is the caller's. */",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		f"#include \"{basename}.h\"",
		*(["#include <string.h>"] if any(
			_attr(one, "nonce") is not None
			and nonce_bytes(one, resolved) is not None for one in replays)
		  else []),
		"",
		f"#ifndef {macro(prefix, 'replay', 'verdict', 'defined')}",
		f"#define {macro(prefix, 'replay', 'verdict', 'defined')}",
		"typedef enum {",
		*(f"\t{macro(prefix, 'replay', one)} = {at}," for at, one
		  in enumerate(VERDICTS)),
		f"}} {verdict};",
		"#endif",
		"",
	]
	for replay in replays:
		if _attr(replay, "counter") is not None:
			lines += _counter(replay, resolved, prefix)
		else:
			lines += _nonce(replay, resolved, clock_unit(schema, replay), prefix)
	lines += [f"#endif /* {guard} */", ""]
	return {f"{basename}_replay.h": "\n".join(lines)}
