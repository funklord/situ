"""The `epoll` driver: a Linux event loop over the rung-6 state machine.

Decision 0033. A driver is an additive artifact -- `--driver epoll` adds
`<name>_epoll.c` (and its header) and changes nothing else -- that pumps the
`--layer drive` state machine. The state machine is unaware of it: it owns
retransmission, correlation and expiry and never reads a clock or a socket,
reaching I/O only through the `situ_io_t` submit vtable. This loop owns the
clock, the socket and the timer arithmetic, which is the division of labour
26.98 was built for.

The shape is `gen-dissector`'s: one optional artifact over the same resolved
schema. It emits nothing where no relation states a retransmission policy,
the same `driven()` gate the drive layer uses.

WHY THIS IS DATAGRAM. The drive layer pushes one view per `on_message` over
bytes the caller owns, and holds no reassembly state (a stream is rung 4's
`_frame.h`, a separate file). So the loop frames nothing: one `recv` is one
message, acquired at offset zero and handed straight to `on_message`. A
stream socket would run the received bytes through the frame reader first,
which is a different driver's concern.

WHY LEVEL-TRIGGERED. One `recv` per wakeup drains one datagram; anything
still queued re-wakes the next `epoll_wait`. Edge-triggered would oblige a
`recv`-until-`EAGAIN` inner loop on every wakeup for no gain here, and add a
wedge-the-loop failure mode if a datagram were ever missed.

WHY THE SUBMIT SWALLOWS EAGAIN. A datagram `send` is all-or-nothing, so a
full send buffer (`EAGAIN`) is a dropped datagram, which the state machine's
own retry budget is exactly what covers. Reporting it up would abort an
exchange the transport is entitled to lose. Only a hard error propagates.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.drive import driven
from situc.codegen.c.names import ident, macro
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate"]


def _reply_view(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> tuple[str, bool]:
	"""The reply message's view accessor, and whether it is the fixed form.

	`on_message` reads the conversation key off a view of the reply, so the
	loop acquires one over the received datagram first. A fixed struct knows
	its own extent and takes `(msg, offset, out)`; a frame takes the received
	length too (`emit.py:_view_acquisition`).
	"""
	reply = relation.params[1]
	struct = resolved.structs[reply.type_name]
	return ident(prefix, reply.type_name, "view"), struct.layout.is_fixed_size


def _shared(prefix: str) -> list[str]:
	"""The clock, the submit vtable and the callback types -- one copy, since
	none of them depends on which relation is being driven."""
	submit = ident(prefix, "epoll", "submit")
	return [
		"/* Where the bytes go: the submit ctx holds the connected datagram",
		" * fd. A datagram send is all-or-nothing, so a full send buffer",
		" * (EAGAIN) is a dropped datagram the retry budget recovers, and an",
		" * interrupt (EINTR) is retried; only a hard error propagates. */",
		f"static situ_err_t {submit}(void *ctx, const uint8_t *data,"
		" uint32_t len)",
		"{",
		f"\t{ident(prefix, 'epoll', 'ctx')}_t *sock = ctx;",
		"\tfor (;;) {",
		"\t\tconst ssize_t sent = send(sock->fd, data, len, 0);",
		"\t\tif (sent >= 0) {",
		"\t\t\treturn SITU_OK;\t/* a datagram is all-or-nothing */",
		"\t\t}",
		"\t\tif (errno == EINTR) {",
		"\t\t\tcontinue;",
		"\t\t}",
		"\t\tif (errno == EAGAIN || errno == EWOULDBLOCK) {",
		"\t\t\treturn SITU_OK;\t/* buffer full: let retransmission recover it */",
		"\t\t}",
		"\t\treturn SITU_ERR_BOUNDS;\t/* a hard error */",
		"\t}",
		"}",
		"",
		f"situ_io_t {ident(prefix, 'epoll', 'io')}"
		f"({ident(prefix, 'epoll', 'ctx')}_t *ctx)",
		"{",
		"\tsitu_io_t io;",
		f"\tio.submit = {submit};",
		"\tio.ctx    = ctx;",
		"\treturn io;",
		"}",
		"",
		"/* The clock, read only here and never by the state machine. The value",
		" * wraps at 2^32 ms and that is fine: every deadline comparison is a",
		" * wrap-safe signed difference, here and inside `step`. */",
		f"static uint32_t {ident(prefix, 'epoll', 'now_ms')}(void)",
		"{",
		"\tstruct timespec ts;",
		"\t(void)clock_gettime(CLOCK_MONOTONIC, &ts);",
		"\tconst uint64_t ms = (uint64_t)ts.tv_sec * 1000u",
		"\t                  + (uint64_t)ts.tv_nsec / 1000000u;",
		"\treturn (uint32_t)ms;",
		"}",
		"",
	]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation."""
	drive   = ident(prefix, "drive", relation.name)
	run     = ident(prefix, "epoll", relation.name, "run")
	step    = f"{drive}_step"
	on_msg  = f"{drive}_on_message"
	view_fn, fixed = _reply_view(relation, resolved, prefix)

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


def _header(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str,
		ready: list[tuple[ast.Relation, tuple[int, int]]]) -> str:
	guard = macro(prefix, basename, "EPOLL_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * The epoll driver: a Linux event loop over rung 6 (decision 0033).",
		" * It pumps the `--layer drive` state machine, owning the socket, the",
		" * clock and the timer arithmetic; the state machine owns everything",
		" * else and never reads either. An additive artifact -- it adds files",
		" * rather than changing them.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		f"#include \"{basename}_drive.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
		"/* The submit context: a connected datagram fd. Build the vtable with",
		" * the accessor below and hand it to the drive machine's `_init`. */",
		"typedef struct {",
		"\tint fd;",
		f"}} {ident(prefix, 'epoll', 'ctx')}_t;",
		"",
		f"situ_io_t {ident(prefix, 'epoll', 'io')}"
		f"({ident(prefix, 'epoll', 'ctx')}_t *ctx);",
		"",
		"/* A correlated reply, and a batch of exchanges that ran out of",
		" * retries. Either callback may be NULL; `user` is threaded through. */",
		f"typedef void (*{ident(prefix, 'epoll', 'reply')}_fn)"
		"(uint32_t id, void *user);",
		f"typedef void (*{ident(prefix, 'epoll', 'expired')}_fn)"
		"(uint32_t count, void *user);",
		"",
	]
	for relation, _policy in ready:
		drive = ident(prefix, "drive", relation.name)
		run   = ident(prefix, "epoll", relation.name, "run")
		lines += [
			f"situ_err_t {run}(int fd, {drive}_t *drive,",
			f"                 {ident(prefix, 'epoll', 'reply')}_fn on_reply,",
			f"                 {ident(prefix, 'epoll', 'expired')}_fn"
			" on_expired,",
			"                 void *user);",
			"",
		]
	lines += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]
	return "\n".join(lines) + "\n"


def _source(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str,
		ready: list[tuple[ast.Relation, tuple[int, int]]]) -> str:
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * The epoll event loop for rung 6 (decision 0033). Linux only:",
		" * epoll(7) is the facility this driver is named for.",
		" */",
		"#define _POSIX_C_SOURCE 200809L",
		"",
		f"#include \"{basename}_epoll.h\"",
		"",
		"#include <sys/epoll.h>",
		"#include <sys/socket.h>",
		"#include <time.h>",
		"#include <errno.h>",
		"#include <string.h>",
		"#include <unistd.h>",
		"",
		*_shared(prefix),
	]
	for relation, _policy in ready:
		lines.extend(_run(relation, resolved, prefix))
	return "\n".join(lines).rstrip("\n") + "\n"


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The epoll driver's header and source, or nothing where no exchange
	states a policy -- the same `driven()` gate the drive layer uses."""
	ready = driven(schema, resolved)
	if not ready:
		return {}
	return {
		f"{basename}_epoll.h": _header(schema, resolved, basename, prefix,
		                               ready),
		f"{basename}_epoll.c": _source(schema, resolved, basename, prefix,
		                               ready),
	}
