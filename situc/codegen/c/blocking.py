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
	"""The event loop for one driven relation: blocking's setup, wait
	and teardown around `driver.loop`."""
	return driver.loop(
		relation, resolved, prefix, "blocking", doc=[], floor=1,
		setup=[

		],
		wait=[
			"\t\t/* No multiplexer: the deadline becomes the socket's receive",
			"\t\t * timeout, so a blocking receive wakes on a datagram or when the",
			"\t\t * next retransmission is due, whichever is first -- EAGAIN below",
			"\t\t * is the timeout. Reset each turn; a zero timeout would mean",
			"\t\t * block forever, hence the floor of one. */",
			"\t\tstruct timeval tv;",
			"\t\ttv.tv_sec  = diff / 1000;",
			"\t\ttv.tv_usec = (diff % 1000) * 1000;",
			"\t\tif (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv) < 0) {",
			"\t\t\trc = SITU_ERR_BOUNDS;",
			"\t\t\tsys_errno = errno;",
			"\t\t\tbreak;",
			"\t\t}",
		],
		teardown=[

		])


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The blocking driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="blocking", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
