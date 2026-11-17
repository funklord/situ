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
from situc.codegen import ACQUIRES_DATAGRAMS, argued_refusal
from situc.relation import key_layout
from situc.codegen.c.names import ident, macro
from situc.relation import Refused
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["driven", "generate", "part_of", "refusals"]


def resend_of(relation: ast.Relation) -> int | None:
	"""`resend_ms` where the exchange states one (26.1039).

	With it, `timeout_ms` is the exchange's total wait: fuzznet resends
	every 600 ms at most three times and gives up at 3 s whatever happened,
	which one per-attempt timeout cannot say. Without it, nothing changes:
	every driver generates exactly what it did before.
	"""
	for attr in relation.attrs:
		value = getattr(attr.value, "value", None)
		if attr.name == "resend_ms" and isinstance(value, int):
			return value
	return None


def part_of(relation: ast.Relation) -> tuple[int, int] | None:
	"""`(resend_part_ms, retries_part)` where the exchange states them.

	A second schedule for once part of the reply is held: then the path
	works, and what is missing was lost rather than slow, so it is asked
	for sooner and more often. fuzznet measured a 960 KB fetch at 20 %
	loss in 8.5 s this way, against 24.5-31.5 s at one 600 ms schedule,
	without resending whole replies on a link slower than 300 ms.
	`wellformed` holds the two together and beside `resend_ms`.
	"""
	found: dict[str, int] = {}
	for attr in relation.attrs:
		value = getattr(attr.value, "value", None)
		if attr.name in ("resend_part_ms", "retries_part") \
				and isinstance(value, int):
			found[attr.name] = value
	if len(found) != 2:
		return None
	return found["resend_part_ms"], found["retries_part"]


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
		# A side that takes a `parameter` (0050). A driver owns the socket
		# and BUILDS a view of each datagram it receives -- so unlike the
		# rungs that are handed views, it must be told the argument, and
		# every driver in every backend acquires through the same `at` or
		# `new` that now takes one. Refused rather than emitted with the
		# argument missing, which is a view of the right bytes under the
		# wrong layout in three backends and a compile error in the fourth.
		#
		# This gate is the one the C drivers, the Qt driver and the asyncio
		# driver all read, so filtering here is what makes the nine of them
		# skip the same relation rather than nine copies of one test.
		if argued_refusal(relation, resolved, ACQUIRES_DATAGRAMS):
			continue
		try:
			key_layout(relation, resolved)
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
		argued = argued_refusal(relation, resolved, ACQUIRES_DATAGRAMS)
		if argued:
			found.append((relation.name, argued))
			continue
		try:
			key_layout(relation, resolved)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def _machine(relation: ast.Relation, policy: tuple[int, int],
		prefix: str) -> list[str]:
	timeout, retries = policy
	resend = resend_of(relation)
	part   = part_of(relation) if resend else None
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
		*([f"#define {macro(prefix, relation.name, 'RESEND_MS')} {resend}u",
		   "/* With a resend interval, TIMEOUT_MS is the whole exchange's",
		   " * wait: a retransmission every RESEND_MS, at most RETRIES of",
		   " * them, and the exchange given up at TIMEOUT_MS whatever",
		   " * happened (26.1039). */"] if resend else []),
		*([f"#define {macro(prefix, relation.name, 'RESEND_PART_MS')} {part[0]}u",
		   f"#define {macro(prefix, relation.name, 'RETRIES_PART')} {part[1]}u",
		   "/* Once part of the reply is held -- `_part_held` says so -- the",
		   " * next retransmission is due RESEND_PART_MS after the last send,",
		   " * and RETRIES_PART bounds them all, the ones already sent",
		   " * included: the path works, so what is missing was lost. */"]
		  if part else []),
		"",
		"typedef struct {",
		"\tconst uint8_t *bytes;\t/* yours; kept for the retransmission */",
		"\tuint32_t len;",
		"\tuint32_t id;",
		"\tuint32_t deadline_ms;",
		*(["\tuint32_t end_ms;\t\t/* the whole exchange's end */"]
		  if resend else []),
		*(["\tuint32_t sent_ms;\t/* the last send, which the part schedule"
		   " counts from */",
		   "\tuint8_t  part;\t\t/* part of the reply is held */"]
		  if part else []),
		"\tuint32_t left;\t\t/* retransmissions remaining */",
		"\tuint32_t retries;\t/* this exchange's own, as sent */",
		*(["\tuint32_t resend_ms;"] if resend else []),
		*(["\tuint32_t resend_part_ms;", "\tuint32_t retries_part;"]
		  if part else []),
		"\tuint8_t  live;",
		f"}} {name}_slot_t;",
		"",
		"/* One exchange's own numbers, where they are not the relation's:",
		" * a fetch with its own deadline, a wait with no resends because",
		" * another host sent the request (fuzznet, 26.1055). NULL takes",
		" * the driver's, which `init` set from the schema. */",
		"typedef struct {",
		"\tuint32_t timeout_ms;",
		"\tuint32_t retries;",
		*(["\tuint32_t resend_ms;"] if resend else []),
		*(["\tuint32_t resend_part_ms;", "\tuint32_t retries_part;"]
		  if part else []),
		f"}} {name}_policy_t;",
		"",
		"typedef struct {",
		f"\t{name}_slot_t *slots;",
		"\tuint32_t cap;",
		"\tuint32_t timeout_ms;",
		"\tuint32_t retries;",
		*(["\tuint32_t resend_ms;"] if resend else []),
		*(["\tuint32_t resend_part_ms;", "\tuint32_t retries_part;"]
		  if part else []),
		f"\t{table}_t pending;",
		"\tsitu_io_t io;",
		"\t/* Optional: told which exchange a retransmission is, so a",
		"\t * caller that seals each send can reseal it (fuzznet, 26.1036).",
		"\t * NULL sends the same bytes again through `io.submit`. */",
		"\tsitu_err_t (*resend)(void *ctx, uint32_t id, const uint8_t *data,",
		"\t                     uint32_t len);",
		"\t/* Optional: told each exchange that expires, by id, as `step`",
		"\t * drops it -- the count alone leaves a caller holding many",
		"\t * unable to say which (fuzznet, 26.1055). */",
		"\tvoid (*expire)(void *ctx, uint32_t id);",
		f"}} {name}_t;",
		"",
		*([f"/* The sooner of two deadlines, serially from `now_ms`: the clock",
		   " * wraps, and so do the deadlines (26.1008). */",
		   f"static inline uint32_t {name}_sooner(uint32_t a, uint32_t b,",
		   "                                     uint32_t now_ms)",
		   "{",
		   "\treturn (uint32_t)(a - now_ms) < (uint32_t)(b - now_ms) ? a : b;",
		   "}",
		   ""] if resend else []),
		f"static inline void {name}_init({name}_t *drive,",
		f"                               {name}_slot_t *slots,",
		f"                               {table}_slot_t *keys, uint32_t cap,",
		"                               situ_io_t io, uint32_t timeout_ms,",
		("                               uint32_t retries, uint32_t resend_ms,\n"
		 "                               uint32_t resend_part_ms,"
		 " uint32_t retries_part)"
		 if part else
		 "                               uint32_t retries, uint32_t resend_ms)"
		 if resend else
		 "                               uint32_t retries)"),
		"{",
		*(["\tdrive->resend_ms  = resend_ms;"] if resend else []),
		*(["\tdrive->resend_part_ms = resend_part_ms;",
		   "\tdrive->retries_part   = retries_part;"] if part else []),
		"\tdrive->slots      = slots;",
		"\tdrive->cap        = cap;",
		"\tdrive->timeout_ms = timeout_ms;",
		"\tdrive->retries    = retries;",
		"\tdrive->io         = io;",
		"\tdrive->resend     = NULL;",
		"\tdrive->expire     = NULL;",
		f"\t{table}_init(&drive->pending, keys, cap);",
		"\tfor (uint32_t i = 0u; i < cap; i++) {",
		"\t\tslots[i].live = 0u;",
		"\t}",
		"}",
		"",
		"/* Have retransmissions go through `resend`, which is told the",
		" * exchange's id -- so a caller that seals every send under a fresh",
		" * nonce can reseal the retry rather than replay it (26.1036). */",
		f"static inline void {name}_on_resend({name}_t *drive,",
		"\tsitu_err_t (*resend)(void *ctx, uint32_t id, const uint8_t *data,",
		"\t                     uint32_t len))",
		"{",
		"\tdrive->resend = resend;",
		"}",
		"",
		f"/* Be told which exchanges expire, by id (26.1055). */",
		f"static inline void {name}_on_expire({name}_t *drive,",
		"\tvoid (*expire)(void *ctx, uint32_t id))",
		"{",
		"\tdrive->expire = expire;",
		"}",
		"",
		f"/* Remember `{first.name}` until it is answered, without sending it:",
		" * for a caller that sent it already, sealed under its own id, or one",
		" * that only awaits it because another host sent it (26.1055).",
		" * `policy` NULL takes the driver's numbers.",
		" *",
		" * THE BYTES ARE YOURS AND MUST NOT MOVE until the exchange ends: a",
		" * retransmission sends them again. Nothing is copied and nothing is",
		" * allocated. */",
		f"static inline situ_err_t {name}_track_with({name}_t *drive,",
		f"                                           situ_view_t {first.name},",
		"                                           const uint8_t *bytes,",
		"                                           uint32_t len, uint32_t id,",
		"                                           uint32_t now_ms,",
		f"                                           const {name}_policy_t"
		" *policy)",
		"{",
		f"\tconst situ_err_t err = {table}_record(&drive->pending, "
		f"{first.name}, id);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"\tconst uint32_t timeout_ms = policy != NULL ? policy->timeout_ms"
		" : drive->timeout_ms;",
		"\tconst uint32_t retries    = policy != NULL ? policy->retries"
		" : drive->retries;",
		*(["\tconst uint32_t resend_ms  = policy != NULL ? policy->resend_ms"
		   " : drive->resend_ms;"] if resend else []),
		"",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live == 0u) {",
		"\t\t\tdrive->slots[i].bytes       = bytes;",
		"\t\t\tdrive->slots[i].len         = len;",
		"\t\t\tdrive->slots[i].id          = id;",
		*(["\t\t\tdrive->slots[i].end_ms      = now_ms + timeout_ms;",
		   "\t\t\tdrive->slots[i].deadline_ms = retries == 0u",
		   "\t\t\t\t? drive->slots[i].end_ms",
		   f"\t\t\t\t: {name}_sooner(now_ms + resend_ms,",
		   "\t\t\t\t              drive->slots[i].end_ms, now_ms);",
		   "\t\t\tdrive->slots[i].resend_ms   = resend_ms;"]
		  if resend else
		  ["\t\t\tdrive->slots[i].deadline_ms = now_ms + timeout_ms;"]),
		"\t\t\tdrive->slots[i].left        = retries;",
		"\t\t\tdrive->slots[i].retries     = retries;",
		*(["\t\t\tdrive->slots[i].sent_ms     = now_ms;",
		   "\t\t\tdrive->slots[i].part        = 0u;",
		   "\t\t\tdrive->slots[i].resend_part_ms = policy != NULL",
		   "\t\t\t\t? policy->resend_part_ms : drive->resend_part_ms;",
		   "\t\t\tdrive->slots[i].retries_part   = policy != NULL",
		   "\t\t\t\t? policy->retries_part : drive->retries_part;"]
		  if part else []),
		"\t\t\tdrive->slots[i].live        = 1u;",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\t/* Refused, so not pending either (26.1054). */",
		f"\t{table}_forget(&drive->pending, id);",
		"\treturn SITU_ERR_BOUNDS;",
		"}",
		"",
		f"static inline situ_err_t {name}_track({name}_t *drive,",
		f"                                      situ_view_t {first.name},",
		"                                      const uint8_t *bytes,",
		"                                      uint32_t len, uint32_t id,",
		"                                      uint32_t now_ms)",
		"{",
		f"\treturn {name}_track_with(drive, {first.name}, bytes, len, id,"
		" now_ms, NULL);",
		"}",
		"",
		f"/* Send `{first.name}`, and remember it until it is answered: `_track`",
		" * and then the first send -- through the `on_resend` hook where one",
		" * is set, so a caller that seals every send under the exchange's",
		" * id seals the first one too (26.1055), and `io.submit` otherwise. */",
		f"static inline situ_err_t {name}_send_with({name}_t *drive,",
		f"                                          situ_view_t {first.name},",
		"                                          const uint8_t *bytes,",
		"                                          uint32_t len, uint32_t id,",
		"                                          uint32_t now_ms,",
		f"                                          const {name}_policy_t"
		" *policy)",
		"{",
		f"\tconst situ_err_t err = {name}_track_with(drive, {first.name},"
		" bytes, len, id,",
		"\t                                          now_ms, policy);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"\treturn drive->resend != NULL",
		"\t\t? drive->resend(drive->io.ctx, id, bytes, len)",
		"\t\t: drive->io.submit(drive->io.ctx, bytes, len);",
		"}",
		"",
		f"static inline situ_err_t {name}_send({name}_t *drive,",
		f"                                     situ_view_t {first.name},",
		"                                     const uint8_t *bytes,",
		"                                     uint32_t len, uint32_t id,",
		"                                     uint32_t now_ms)",
		"{",
		f"\treturn {name}_send_with(drive, {first.name}, bytes, len, id,"
		" now_ms, NULL);",
		"}",
		"",
		"/* End exchange `id` without a reply to match: for a caller that",
		" * matches replies itself, or closes the path they would come by",
		" * (fuzznet, 26.1055). SITU_ERR_CONSTRAINT where none is pending. */",
		f"static inline situ_err_t {name}_end({name}_t *drive, uint32_t id)",
		"{",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live != 0u && drive->slots[i].id == id) {",
		"\t\t\tdrive->slots[i].live = 0u;",
		f"\t\t\t{table}_forget(&drive->pending, id);",
		"\t\t\treturn SITU_OK;",
		"\t\t}",
		"\t}",
		"\treturn SITU_ERR_CONSTRAINT;",
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
		f"/* `_on_message` without ending the exchange: which request",
		" * `" + second.name + "` answers, for a reply in pieces (26.1009). */",
		f"static inline situ_err_t {name}_peek(const {name}_t *drive,",
		f"                                     situ_view_t {second.name},",
		"                                     uint32_t *id)",
		"{",
		f"\treturn {table}_peek(&drive->pending, {second.name}, id);",
		"}",
		"",
		*_part_held(name, part),
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
		# Serially, modulo 2^32: `now_ms` is a millisecond counter that
		# wraps, and `>=` across the wrap expired an exchange the moment
		# it was sent -- fuzznet, after 49.7 days of uptime (26.1008).
		"\t\tif ((uint32_t)(now_ms - drive->slots[i].deadline_ms)"
		" < 0x80000000u) {",
		("\t\t\tif (drive->slots[i].left == 0u"
		 "\n\t\t\t    || (uint32_t)(now_ms - drive->slots[i].end_ms)"
		 " < 0x80000000u) {"
		 if resend else
		 "\t\t\tif (drive->slots[i].left == 0u) {"),
		"\t\t\t\tdrive->slots[i].live = 0u;",
		# The key too: the table is the conversation's, and a slot the
		# driver dropped while the table held its key leaked one entry
		# per expiry until `send` refused everything (26.1054).
		f"\t\t\t\t{table}_forget(&drive->pending,"
		" drive->slots[i].id);",
		"\t\t\t\tif (drive->expire != NULL) {",
		"\t\t\t\t\tdrive->expire(drive->io.ctx, drive->slots[i].id);",
		"\t\t\t\t}",
		"\t\t\t\t*expired += 1u;",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tdrive->slots[i].left        -= 1u;",
		*(["\t\t\tdrive->slots[i].sent_ms      = now_ms;",
		   "\t\t\tdrive->slots[i].deadline_ms  = drive->slots[i].left == 0u",
		   "\t\t\t\t? drive->slots[i].end_ms",
		   f"\t\t\t\t: {name}_sooner(now_ms + (drive->slots[i].part != 0u",
		   "\t\t\t\t                          ? drive->slots[i].resend_part_ms",
		   "\t\t\t\t                          : drive->slots[i].resend_ms),",
		   "\t\t\t\t              drive->slots[i].end_ms, now_ms);"]
		  if part else
		  ["\t\t\tdrive->slots[i].deadline_ms  = drive->slots[i].left == 0u",
		   "\t\t\t\t? drive->slots[i].end_ms",
		   f"\t\t\t\t: {name}_sooner(now_ms + drive->slots[i].resend_ms,",
		   "\t\t\t\t              drive->slots[i].end_ms, now_ms);"]
		  if resend else
		  ["\t\t\tdrive->slots[i].deadline_ms  = now_ms + drive->timeout_ms;"]),
		"\t\t\tif (drive->resend != NULL) {",
		"\t\t\t\t(void)drive->resend(drive->io.ctx, drive->slots[i].id,",
		"\t\t\t\t                     drive->slots[i].bytes,",
		"\t\t\t\t                     drive->slots[i].len);",
		"\t\t\t} else {",
		"\t\t\t\t(void)drive->io.submit(drive->io.ctx,",
		"\t\t\t\t                       drive->slots[i].bytes,",
		"\t\t\t\t                       drive->slots[i].len);",
		"\t\t\t}",
		"\t\t}",
		"\t\tif (any == 0u || (uint32_t)(drive->slots[i].deadline_ms - now_ms)"
		" < (uint32_t)(soonest - now_ms)) {",
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


def _part_held(name: str, part: tuple[int, int] | None) -> list[str]:
	"""Told that part of exchange `id`'s reply has arrived.

	Only the consumer knows: the driver sees requests and whole replies,
	and reassembly is the consumer's. The first call switches the exchange
	to the part schedule -- its next resend due `resend_part_ms` after the
	last send, at once if that has passed, and `retries_part` bounding every
	resend it has had or will have. Later calls change nothing, since the
	schedule counts from the last send rather than from the latest piece.
	"""
	if part is None:
		return []
	return [
		"/* Part of exchange `id`'s reply is held: from here its next",
		" * retransmission is due RESEND_PART_MS after the last send, and",
		" * RETRIES_PART bounds them all. SITU_ERR_CONSTRAINT where no such",
		" * exchange is outstanding. */",
		f"static inline situ_err_t {name}_part_held({name}_t *drive,",
		"                                          uint32_t id, uint32_t now_ms)",
		"{",
		"\tfor (uint32_t i = 0u; i < drive->cap; i++) {",
		"\t\tif (drive->slots[i].live == 0u || drive->slots[i].id != id) {",
		"\t\t\tcontinue;",
		"\t\t}",
		"\t\tif (drive->slots[i].part == 0u) {",
		"\t\t\tconst uint32_t done = drive->slots[i].retries"
		" - drive->slots[i].left;",
		"\t\t\tuint32_t       due  = drive->slots[i].sent_ms"
		" + drive->slots[i].resend_part_ms;",
		"",
		"\t\t\tif ((uint32_t)(now_ms - due) < 0x80000000u) {",
		"\t\t\t\tdue = now_ms;\t/* already due: `sooner` counts forward */",
		"\t\t\t}",
		"\t\t\tdrive->slots[i].part        = 1u;",
		"\t\t\tdrive->slots[i].left        = drive->slots[i].retries_part > done",
		"\t\t\t\t? drive->slots[i].retries_part - done : 0u;",
		"\t\t\tdrive->slots[i].deadline_ms = drive->slots[i].left == 0u",
		"\t\t\t\t? drive->slots[i].end_ms",
		f"\t\t\t\t: {name}_sooner(due, drive->slots[i].end_ms, now_ms);",
		"\t\t}",
		"\t\treturn SITU_OK;",
		"\t}",
		"\treturn SITU_ERR_CONSTRAINT;",
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
		f"/* Generated by situc {__version__} from {basename}.situ -- do not edit.",
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
