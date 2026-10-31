"""The `poll` driver: a POSIX readiness event loop over the rung-6 machine.

Decision 0033. A driver is an additive artifact -- `--driver poll` adds
`<name>_poll.c` (and its header) and changes nothing else -- that pumps the
`--layer drive` state machine. It is the epoll driver's shape (they are both
readiness facilities) with one difference: `poll` has no persistent
registration, so the loop builds a one-entry `struct pollfd` each turn and
passes the deadline as the wait timeout. `poll` is POSIX rather than Linux,
with no `FD_SETSIZE` ceiling, which is why it is the smallest real
multiplexer worth shipping (0033).

The division of labour is the drive layer's: the loop owns the socket, the
clock and the timer arithmetic; the state machine owns retransmission,
correlation and expiry and reaches I/O only through the `situ_io_t` submit
vtable. One recv per wakeup, one datagram per message, no framing -- a stream
is rung 4's `_frame.h`, a different driver's concern. The submit swallows
`EAGAIN` as a dropped datagram the retry budget recovers.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c import driver
from situc.resolve import ResolvedSchema

__all__ = ["generate"]

_HEADER_DOC = [
	" * The poll driver: a POSIX event loop over rung 6 (decision 0033).",
	" * It pumps the `--layer drive` state machine, owning the socket, the",
	" * clock and the timer arithmetic; the state machine owns everything",
	" * else and never reads either. An additive artifact -- it adds files",
	" * rather than changing them.",
]

_SOURCE_DOC = [
	" * The poll(2) event loop for rung 6 (decision 0033). POSIX.",
]

_INCLUDES = [
	"#include <poll.h>",
	"#include <sys/socket.h>",
	"#include <time.h>",
	"#include <errno.h>",
	"#include <string.h>",
	"#include <unistd.h>",
]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation: poll's setup, wait
	and teardown around `driver.loop`."""
	return driver.loop(
		relation, resolved, prefix, "poll", doc=[],
		setup=[

		],
		wait=[
			"\t\t/* No persistent registration: the descriptor set is one entry,",
			"\t\t * rebuilt each turn. That is `poll`'s O(1)-here cost and `epoll`'s",
			"\t\t * whole advantage at scale, which one socket does not feel. */",
			"\t\tstruct pollfd pfd;",
			"\t\tpfd.fd      = fd;",
			"\t\tpfd.events  = POLLIN;",
			"\t\tpfd.revents = 0;",
			"\t\tconst int n = poll(&pfd, 1, (int)diff);",
			"\t\tif (n < 0) {",
			"\t\t\tif (errno == EINTR) {",
			"\t\t\t\tcontinue;",
			"\t\t\t}",
			"\t\t\trc = SITU_ERR_BOUNDS;",
			"\t\t\tsys_errno = errno;",
			"\t\t\tbreak;",
			"\t\t}",
			"\t\tif (n == 0 || (pfd.revents & POLLIN) == 0) {",
			"\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
			"\t\t}",
		],
		teardown=[

		])


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The poll driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="poll", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
