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
	"""The event loop for one driven relation: epoll's setup, wait and
	teardown around `driver.loop`."""
	return driver.loop(
		relation, resolved, prefix, "epoll", doc=[],
		setup=[
			"\tconst int epfd = epoll_create1(EPOLL_CLOEXEC);",
			"\tif (epfd < 0) {",
			"\t\trc = SITU_ERR_BOUNDS;",
			"\t\tsys_errno = errno;",
			"\t\tgoto done;",
			"\t}",
			"\tstruct epoll_event ev;",
			"\tmemset(&ev, 0, sizeof ev);",
			"\tev.events  = EPOLLIN;\t/* level-triggered: one recv per wakeup */",
			"\tev.data.fd = fd;",
			"\tif (epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev) < 0) {",
			"\t\trc = SITU_ERR_BOUNDS;",
			"\t\tsys_errno = errno;",
			"\t\tgoto done;",
			"\t}",
		],
		wait=[
			"\t\tstruct epoll_event out;",
			"\t\tconst int n = epoll_wait(epfd, &out, 1, (int)diff);",
			"\t\tif (n < 0) {",
			"\t\t\tif (errno == EINTR) {",
			"\t\t\t\tcontinue;",
			"\t\t\t}",
			"\t\t\trc = SITU_ERR_BOUNDS;",
			"\t\t\tsys_errno = errno;",
			"\t\t\tbreak;",
			"\t\t}",
			"\t\tif (n == 0 || (out.events & EPOLLIN) == 0u) {",
			"\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
			"\t\t}",
		],
		teardown=[
			"\tif (epfd >= 0) {",
			"\t\t(void)close(epfd);",
			"\t}",
		])


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The epoll driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="epoll", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
