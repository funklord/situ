"""The `select` driver: the oldest readiness loop, and its cliff.

Decision 0033. A driver is an additive artifact -- `--driver select` adds
`<name>_select.c` (and its header) and changes nothing else -- that pumps the
`--layer drive` state machine. `select` is the readiness facility that is
everywhere, POSIX since the beginning, which is the whole of what it has over
`poll`: it is strictly worse wherever `poll` exists. Its descriptor set is a
fixed-size bitmap, so a file descriptor at or past `FD_SETSIZE` cannot be
named at all -- `FD_SET` on one is undefined and corrupts the caller's stack.
That ceiling is a cliff rather than a slope, and this driver makes it an
explicit refusal: a descriptor it cannot watch is `SITU_ERR_BOUNDS` before
the loop starts, not a silent corruption.

The scaffolding it shares with the other fd-based drivers -- the submit
vtable, the clock, the header and the `driven()` gate -- lives in `driver`;
what is here is select's own loop, which rebuilds its one-descriptor set and
its timeout each turn because `select` writes back through both.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c import driver
from situc.codegen.c.names import ident
from situc.resolve import ResolvedSchema

__all__ = ["generate"]

_HEADER_DOC = [
	" * The select driver: a POSIX event loop over rung 6 (decision 0033).",
	" * It pumps the `--layer drive` state machine, owning the socket, the",
	" * clock and the timer arithmetic; the state machine owns everything",
	" * else and never reads either. An additive artifact -- it adds files",
	" * rather than changing them.",
]

_SOURCE_DOC = [
	" * The select(2) event loop for rung 6 (decision 0033). POSIX, and the",
	" * descriptor ceiling FD_SETSIZE is refused rather than corrupted.",
]

_INCLUDES = [
	"#include <sys/select.h>",
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
	run     = ident(prefix, "select", relation.name, "run")
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
		f"                 {ident(prefix, 'select', 'reply')}_fn on_reply,",
		f"                 {ident(prefix, 'select', 'expired')}_fn on_expired,",
		"                 void *user)",
		"{",
		"\t/* select's descriptor set is a fixed bitmap: a descriptor at or",
		"\t * past FD_SETSIZE cannot be named, and FD_SET on one is undefined.",
		"\t * The cliff is refused rather than fallen off. */",
		"\tif (fd < 0 || fd >= FD_SETSIZE) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tsitu_err_t rc = SITU_OK;",
		"\tfor (;;) {",
		f"\t\tconst uint32_t now = {ident(prefix, 'select', 'now_ms')}();",
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
		"\t\t/* The wait timeout is `next_ms - now`, floored at zero and",
		"\t\t * wrap-safe: a future deadline waits, a reached one polls and",
		"\t\t * `step` retransmits on the next turn. */",
		"\t\tint32_t diff = (int32_t)(next_ms - now);",
		"\t\tif (diff < 0) {",
		"\t\t\tdiff = 0;",
		"\t\t}",
		"",
		"\t\t/* Both the set and the timeout are rebuilt each turn: select",
		"\t\t * writes back through both, leaving the set as which descriptors",
		"\t\t * are ready and the timeout as what was left unslept. */",
		"\t\tfd_set rfds;",
		"\t\tFD_ZERO(&rfds);",
		"\t\tFD_SET(fd, &rfds);",
		"\t\tstruct timeval tv;",
		"\t\ttv.tv_sec  = diff / 1000;",
		"\t\ttv.tv_usec = (diff % 1000) * 1000;",
		"\t\tconst int n = select(fd + 1, &rfds, NULL, NULL, &tv);",
		"\t\tif (n < 0) {",
		"\t\t\tif (errno == EINTR) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif (n == 0) {",
		"\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
		"\t\t}",
		"",
		"\t\tif (FD_ISSET(fd, &rfds)) {",
		"\t\t\tuint8_t buf[2048];",
		"\t\t\tconst ssize_t got = recv(fd, buf, sizeof buf, 0);",
		"\t\t\tif (got < 0) {",
		"\t\t\t\tif (errno == EINTR || errno == EAGAIN",
		"\t\t\t\t                || errno == EWOULDBLOCK) {",
		"\t\t\t\t\tcontinue;",
		"\t\t\t\t}",
		"\t\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t\tif (got == 0) {",
		"\t\t\t\tcontinue;\t/* an empty datagram is not a message */",
		"\t\t\t}",
		"",
		"\t\t\t/* One datagram is one message: acquire a view at offset",
		"\t\t\t * zero and hand it straight to the state machine. A frame",
		"\t\t\t * too short to be the reply is dropped. */",
		"\t\t\tsitu_msg_t  msg;",
		"\t\t\tsitu_view_t reply;",
		"\t\t\tuint32_t    id = 0u;",
		"\t\t\tsitu_msg_init(&msg, buf, (uint32_t)got);",
		f"\t\t\tif ({acquire} != SITU_OK) {{",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		f"\t\t\tif ({on_msg}(drive, reply, &id) == SITU_OK"
		" && on_reply != NULL) {",
		"\t\t\t\ton_reply(id, user);",
		"\t\t\t}",
		"\t\t}",
		"\t}",
		"",
		"\treturn rc;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The select driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="select", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
