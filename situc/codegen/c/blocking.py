"""The `blocking` driver: one socket, `recv`/`send`, no multiplexer.

Decision 0033. A driver is an additive artifact -- `--driver blocking` adds
`<name>_blocking.c` (and its header) and changes nothing else -- that pumps
the `--layer drive` state machine. It is the proof the machine drives with no
OS facility beyond `read` and `write`: there is nothing to multiplex when
there is one socket, so the loop simply blocks in `recv` until a datagram
arrives or the state machine's next deadline elapses, whichever comes first.

The deadline becomes the socket's receive timeout (`SO_RCVTIMEO`, reset each
turn), which is how a driver with no `poll` still hands `step`'s deadline to
the kernel. Everything else is the drive layer's usual division of labour:
the loop owns the socket, the clock and the timer; the state machine owns
retransmission, correlation and expiry and reaches I/O only through the
`situ_io_t` submit vtable. One recv is one message, no framing. The submit
swallows `EAGAIN` as a dropped datagram the retry budget recovers.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c import driver
from situc.codegen.c.names import ident
from situc.resolve import ResolvedSchema

__all__ = ["generate"]

_HEADER_DOC = [
	" * The blocking driver: an event loop over rung 6 with no OS",
	" * multiplexer (decision 0033). It pumps the `--layer drive` state",
	" * machine, owning the socket, the clock and the timer arithmetic;",
	" * the state machine owns everything else and never reads either. An",
	" * additive artifact -- it adds files rather than changing them.",
]

_SOURCE_DOC = [
	" * The blocking event loop for rung 6 (decision 0033). One socket,",
	" * `recv`/`send`, the deadline as the receive timeout.",
]

_INCLUDES = [
	"#include <sys/socket.h>",
	"#include <sys/time.h>",
	"#include <time.h>",
	"#include <errno.h>",
	"#include <string.h>",
	"#include <unistd.h>",
]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation."""
	drive   = ident(prefix, "drive", relation.name)
	run     = ident(prefix, "blocking", relation.name, "run")
	step    = f"{drive}_step"
	on_msg  = f"{drive}_on_message"
	view_fn, fixed = driver.reply_view(relation, resolved, prefix)

	acquire = (f"{view_fn}(&msg, 0u, &reply)" if fixed
	           else f"{view_fn}(&msg, 0u, (uint32_t)got, &reply)")

	return [
		f"/* Drive `{relation.name}` until it is answered or expires.",
		" *",
		" * `on_reply` fires with the caller's handle when a reply correlates,",
		" * `on_expired` with a count when exchanges run out of retries; either",
		" * may be NULL. The loop returns when nothing is outstanding -- `step`",
		" * answers SITU_ERR_TRUNCATED, which is completion, not an error. */",
		f"situ_err_t {run}(int fd, {drive}_t *drive,",
		f"                 {ident(prefix, 'blocking', 'reply')}_fn on_reply,",
		f"                 {ident(prefix, 'blocking', 'expired')}_fn"
		" on_expired,",
		"                 void *user)",
		"{",
		"\tsitu_err_t rc = SITU_OK;",
		"\tfor (;;) {",
		f"\t\tconst uint32_t now = {ident(prefix, 'blocking', 'now_ms')}();",
		"\t\tuint32_t next_ms = 0u;",
		"\t\tuint32_t expired = 0u;",
		"",
		"\t\t/* Retransmit what is due, expire what is spent, and learn the",
		"\t\t * earliest remaining deadline. TRUNCATED is an empty in-flight",
		"\t\t * set: no deadline to wait on and the exchange is done. */",
		f"\t\tconst situ_err_t stepped = {step}(drive, now, &next_ms,"
		" &expired);",
		"\t\tif (expired != 0u && on_expired != NULL) {",
		"\t\t\ton_expired(expired, user);",
		"\t\t}",
		"\t\tif (stepped == SITU_ERR_TRUNCATED) {",
		"\t\t\trc = SITU_OK;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif (stepped != SITU_OK) {",
		"\t\t\trc = stepped;",
		"\t\t\tbreak;",
		"\t\t}",
		"",
		"\t\t/* No multiplexer: the deadline becomes the socket's receive",
		"\t\t * timeout, so a blocking `recv` wakes on a datagram or when the",
		"\t\t * next retransmission is due, whichever is first. Reset each",
		"\t\t * turn, wrap-safe. A zero difference would mean block forever,",
		"\t\t * so it is floored to a poll rather than a wait. */",
		"\t\tint32_t diff = (int32_t)(next_ms - now);",
		"\t\tif (diff < 1) {",
		"\t\t\tdiff = 1;",
		"\t\t}",
		"\t\tstruct timeval tv;",
		"\t\ttv.tv_sec  = diff / 1000;",
		"\t\ttv.tv_usec = (diff % 1000) * 1000;",
		"\t\tif (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv) < 0) {",
		"\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\tbreak;",
		"\t\t}",
		"",
		"\t\tuint8_t buf[2048];",
		"\t\tconst ssize_t got = recv(fd, buf, sizeof buf, 0);",
		"\t\tif (got < 0) {",
		"\t\t\tif (errno == EINTR || errno == EAGAIN",
		"\t\t\t                || errno == EWOULDBLOCK) {",
		"\t\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
		"\t\t\t}",
		"\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif (got == 0) {",
		"\t\t\tcontinue;\t/* an empty datagram is not a message */",
		"\t\t}",
		"",
		"\t\t/* One datagram is one message: acquire a view at offset zero",
		"\t\t * and hand it straight to the state machine. A frame too short",
		"\t\t * to be the reply is dropped. */",
		"\t\tsitu_msg_t  msg;",
		"\t\tsitu_view_t reply;",
		"\t\tuint32_t    id = 0u;",
		"\t\tsitu_msg_init(&msg, buf, (uint32_t)got);",
		f"\t\tif ({acquire} != SITU_OK) {{",
		"\t\t\tcontinue;",
		"\t\t}",
		f"\t\tif ({on_msg}(drive, reply, &id) == SITU_OK"
		" && on_reply != NULL) {",
		"\t\t\ton_reply(id, user);",
		"\t\t}",
		"\t}",
		"",
		"\treturn rc;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The blocking driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="blocking", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
