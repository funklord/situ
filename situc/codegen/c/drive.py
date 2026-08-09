"""Retransmission and timeout in C: rung 6 of the layer ladder (26.98).

The last rung, and the one whose permission is owning I/O. It owns nothing
else -- and in particular it never owns the clock.

**Time is a parameter.** Every entry point takes `now_ms` and `step` returns
the next deadline. That is decision 0033's core requirement, and it is what
makes a timeout bug reproduce: ten simulated minutes run in a loop with no
sleep in them, byte for byte the same on every run. A wall clock inside the
state machine would make every test of it a race, which this family has paid
for once already.

**`step` returns the next deadline** because every multiplexing facility
takes a timeout and only the state machine knows when it next needs waking.
Without it each driver invents a polling interval and the timing contract
quietly stops being the schema's (0033).

WHERE THE IN-FLIGHT BUFFER QUESTION LANDED. 0033 named it and declined to
pick: a completion vtable means something other than the caller owns a buffer
while an operation is in flight, and a view is a base and a limit over memory
the caller owns. Of the three candidates it listed, this takes the simplest:
**an in-flight buffer is not viewed at all.** The state machine never holds a
view across a submit -- it holds the *bytes* to retransmit, which are the
caller's and which the caller promises not to move -- and inbound messages
are pushed in by the caller, who owns those bytes too. Nothing here views
memory it does not control, so 12.3 has nothing to invalidate and the
question does not arise at this rung. A driver that hands buffers to a kernel
will have to answer it properly; this one does not have to, and saying so is
better than pretending it was solved.
"""

from __future__ import annotations

from situc import ast
from situc.relation import key_sides
from situc.codegen.c.names import ident, macro
from situc.relation import Refused
from situc.resolve import ResolvedSchema

__all__ = ["driven", "generate", "refusals"]


def policy_of(relation: ast.Relation) -> tuple[int, int] | None:
	"""`(timeout_ms, retries)` where the exchange states one.

	`wellformed` has already refused half a policy and a non-positive value,
	so either both are here and usable or neither is.
	"""
	found: dict[str, int] = {}
	for attr in relation.attrs:
		value = getattr(attr.value, "value", None)
		if attr.name in ("timeout_ms", "retries") and isinstance(value, int):
			found[attr.name] = value
	if len(found) != 2:
		return None
	return (found["timeout_ms"], found["retries"])


def driven(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[ast.Relation, tuple[int, int]]]:
	"""Every relation that states a policy and can carry a table."""
	ready = []
	for relation in schema.relations():
		policy = policy_of(relation)
		if policy is None:
			continue
		try:
			key_sides(relation, resolved)
		except Refused:
			continue
		ready.append((relation, policy))
	return ready


def refusals(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[str, str]]:
	"""Every relation that states a policy and still gets no driver."""
	found = []
	for relation in schema.relations():
		if policy_of(relation) is None:
			continue
		try:
			key_sides(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def _machine(relation: ast.Relation, policy: tuple[int, int],
		prefix: str) -> list[str]:
	timeout, retries = policy
	name  = ident(prefix, "drive", relation.name)
	table = ident(prefix, "conv", relation.name)
	first, second = relation.params

	return [
		f"/* Retransmission for `{relation.name}`.",
		" *",
		f" * The schema states {timeout} ms and {retries} retries; `init` takes",
		" * both so a deployment can override the numbers it was given. It",
		" * cannot introduce a policy that is not there -- a relation stating",
		" * none generates none of this (decision 0032).",
		" *",
		" * NOTHING HERE READS A CLOCK. Every entry point takes `now_ms`, and",
		" * `step` says when it next needs waking.",
		" */",
		f"#define {macro(prefix, relation.name, 'TIMEOUT_MS')} {timeout}u",
		f"#define {macro(prefix, relation.name, 'RETRIES')} {retries}u",
		"",
		"typedef struct {",
		"\tconst uint8_t *bytes;\t/* yours; kept for the retransmission */",
		"\tuint32_t len;",
		"\tuint32_t id;",
		"\tuint32_t deadline_ms;",
		"\tuint32_t left;\t\t/* retransmissions remaining */",
		"\tuint8_t  live;",
		f"}} {name}_slot_t;",
		"",
		"typedef struct {",
		f"\t{name}_slot_t *slots;",
		"\tuint32_t cap;",
		"\tuint32_t timeout_ms;",
		"\tuint32_t retries;",
		f"\t{table}_t pending;",
		"\tsitu_io_t io;",
		f"}} {name}_t;",
		"",
		f"static inline void {name}_init({name}_t *drive,",
		f"                               {name}_slot_t *slots,",
		f"                               {table}_slot_t *keys, uint32_t cap,",
		"                               situ_io_t io, uint32_t timeout_ms,",
		"                               uint32_t retries)",
		"{",
		"\tdrive->slots      = slots;",
		"\tdrive->cap        = cap;",
		"\tdrive->timeout_ms = timeout_ms;",
		"\tdrive->retries    = retries;",
		"\tdrive->io         = io;",
		f"\t{table}_init(&drive->pending, keys, cap);",
		"\tfor (uint32_t i = 0u; i < cap; i++) {",
		"\t\tslots[i].live = 0u;",
		"\t}",
		"}",
		"",
		f"/* Send `{first.name}`, and remember it until it is answered.",
		" *",
		" * THE BYTES ARE YOURS AND MUST NOT MOVE until the exchange ends: a",
		" * retransmission sends them again. Nothing is copied and nothing is",
		" * allocated. */",
		f"static inline situ_err_t {name}_send({name}_t *drive,",
		f"                                     situ_view_t {first.name},",
		"                                     const uint8_t *bytes,",
		"                                     uint32_t len, uint32_t id,",
		"                                     uint32_t now_ms)",
		"{",
		f"\tconst situ_err_t err = {table}_record(&drive->pending, "
		f"{first.name}, id);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live == 0u) {",
		"\t\t\tdrive->slots[i].bytes       = bytes;",
		"\t\t\tdrive->slots[i].len         = len;",
		"\t\t\tdrive->slots[i].id          = id;",
		"\t\t\tdrive->slots[i].deadline_ms = now_ms + drive->timeout_ms;",
		"\t\t\tdrive->slots[i].left        = drive->retries;",
		"\t\t\tdrive->slots[i].live        = 1u;",
		"\t\t\treturn drive->io.submit(drive->io.ctx, bytes, len);",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_BOUNDS;",
		"}",
		"",
		f"/* A `{second.name}` arrived. SITU_OK and `*id` where it answers "
		"something",
		" * outstanding, SITU_ERR_CONSTRAINT where it does not -- which is a",
		" * duplicate, a reply to an exchange that timed out, or one nobody",
		" * opened. The bytes are yours; nothing here holds a view past the",
		" * call. */",
		f"static inline situ_err_t {name}_on_message({name}_t *drive,",
		f"                                           situ_view_t {second.name},",
		"                                           uint32_t *id)",
		"{",
		f"\tconst situ_err_t err = {table}_match(&drive->pending, "
		f"{second.name}, id);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live != 0u && drive->slots[i].id == *id) {",
		"\t\t\tdrive->slots[i].live = 0u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		"/* Retransmit whatever is due, and say when to come back.",
		" *",
		" * `*next_ms` is the earliest deadline still outstanding, which is what",
		" * every multiplexing facility wants as its timeout. SITU_ERR_TRUNCATED",
		" * means nothing is outstanding and there is no deadline to wait for.",
		" *",
		" * An exchange out of retries is dropped and reported through",
		" * `*expired`, because a caller that is never told has an exchange it",
		" * waits on for ever. */",
		f"static inline situ_err_t {name}_step({name}_t *drive, uint32_t now_ms,",
		"                                     uint32_t *next_ms,",
		"                                     uint32_t *expired)",
		"{",
		"\tuint32_t soonest = 0u;",
		"\tuint8_t  any     = 0u;",
		"",
		"\t*expired = 0u;",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live == 0u) {",
		"\t\t\tcontinue;",
		"\t\t}",
		"\t\tif (now_ms >= drive->slots[i].deadline_ms) {",
		"\t\t\tif (drive->slots[i].left == 0u) {",
		"\t\t\t\tdrive->slots[i].live = 0u;",
		"\t\t\t\t*expired += 1u;",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tdrive->slots[i].left        -= 1u;",
		"\t\t\tdrive->slots[i].deadline_ms  = now_ms + drive->timeout_ms;",
		"\t\t\t(void)drive->io.submit(drive->io.ctx,",
		"\t\t\t                       drive->slots[i].bytes,",
		"\t\t\t                       drive->slots[i].len);",
		"\t\t}",
		"\t\tif (any == 0u || drive->slots[i].deadline_ms < soonest) {",
		"\t\t\tsoonest = drive->slots[i].deadline_ms;",
		"\t\t\tany     = 1u;",
		"\t\t}",
		"\t}",
		"",
		"\tif (any == 0u) {",
		"\t\treturn SITU_ERR_TRUNCATED;",
		"\t}",
		"\t*next_ms = soonest;",
		"\treturn SITU_OK;",
		"}",
		"",
	]


IO_VTABLE = [
	"/* Where the bytes go. Yours, so a test substitutes a transcript and",
	" * never opens a socket -- which is also how loss, reorder and",
	" * duplication are injected without a network.",
	" *",
	" * The shipped path and the tested path are the same program: they",
	" * differ in what fills this struct and in nothing else (0033).",
	" */",
	"typedef struct {",
	"\tsitu_err_t (*submit)(void *ctx, const uint8_t *data, uint32_t len);",
	"\tvoid *ctx;",
	"} situ_io_t;",
	"",
]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The driver header, or nothing where no exchange states a policy."""
	ready = driven(schema, resolved)
	if not ready:
		return {}

	guard = macro(prefix, basename, "DRIVE_H")
	lines = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Retransmission and timeout: rung 6 of the layer ladder. It owns",
		" * I/O through a vtable you supply and it never owns the clock --",
		" * every entry point takes `now_ms` and `step` says when to come",
		" * back. Nothing here allocates.",
		" *",
		" * A separate header on purpose -- a rung adds files rather than",
		" * changing them (decision 0032).",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		f"#include \"{basename}_converse.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
		*IO_VTABLE,
	]

	for relation, policy in ready:
		lines.extend(_machine(relation, policy, prefix))

	lines += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	return {f"{basename}_drive.h": "\n".join(lines) + "\n"}
