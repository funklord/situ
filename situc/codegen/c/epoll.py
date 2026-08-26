"""The `epoll` driver: a Linux readiness event loop over the rung-6 machine.

Decision 0033. A driver is an additive artifact -- `--driver epoll` adds
`<name>_epoll.c` (and its header) and changes nothing else -- that pumps the
`--layer drive` state machine. The state machine is unaware of it: it owns
retransmission, correlation and expiry and never reads a clock or a socket,
reaching I/O only through the `situ_io_t` submit vtable. This loop owns the
clock, the socket and the timer arithmetic, which is the division of labour
26.98 was built for.

The scaffolding it shares with the other fd-based drivers -- the submit
vtable, the clock, the header and the `driven()` gate -- lives in `driver`;
what is here is epoll's own event loop.

WHY THIS IS DATAGRAM. The drive layer pushes one view per `on_message` over
bytes the caller owns, and holds no reassembly state (a stream is rung 4's
`_frame.h`, a separate file). So the loop frames nothing: one `recv` is one
message, acquired at offset zero and handed straight to `on_message`.

WHY LEVEL-TRIGGERED. One `recv` per wakeup drains one datagram; anything
still queued re-wakes the next `epoll_wait`. Edge-triggered would oblige a
`recv`-until-`EAGAIN` inner loop on every wakeup for no gain here, and add a
wedge-the-loop failure mode if a datagram were ever missed.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c import driver
from situc.codegen.c.names import ident
from situc.resolve import ResolvedSchema

__all__ = ["generate"]

_HEADER_DOC = [
	" * The epoll driver: a Linux event loop over rung 6 (decision 0033).",
	" * It pumps the `--layer drive` state machine, owning the socket, the",
	" * clock and the timer arithmetic; the state machine owns everything",
	" * else and never reads either. An additive artifact -- it adds files",
	" * rather than changing them.",
]

_SOURCE_DOC = [
	" * The epoll event loop for rung 6 (decision 0033). Linux only:",
	" * epoll(7) is the facility this driver is named for.",
]

_INCLUDES = [
	"#include <sys/epoll.h>",
	"#include <sys/socket.h>",
	"#include <time.h>",
	"#include <errno.h>",
	"#include <string.h>",
	"#include <unistd.h>",
]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation."""
	drive   = ident(prefix, "drive", relation.name)
	run     = ident(prefix, "epoll", relation.name, "run")
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
		f"                 {ident(prefix, 'epoll', 'reply')}_fn on_reply,",
		f"                 {ident(prefix, 'epoll', 'expired')}_fn on_expired,",
		"                 void *user)",
		"{",
		"\tconst int epfd = epoll_create1(EPOLL_CLOEXEC);",
		"\tif (epfd < 0) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tstruct epoll_event ev;",
		"\tmemset(&ev, 0, sizeof ev);",
		"\tev.events  = EPOLLIN;\t/* level-triggered: one recv per wakeup */",
		"\tev.data.fd = fd;",
		"\tif (epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {",
		"\t\t(void)close(epfd);",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tsitu_err_t rc = SITU_OK;",
		"\tfor (;;) {",
		f"\t\tconst uint32_t now = {ident(prefix, 'epoll', 'now_ms')}();",
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
		"\t\tstruct epoll_event out;",
		"\t\tconst int n = epoll_wait(epfd, &out, 1, (int)diff);",
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
		"\t\tif ((out.events & EPOLLIN) != 0u) {",
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
		"\t(void)close(epfd);",
		"\treturn rc;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The epoll driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="epoll", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
