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
	"""The event loop for one driven relation: select's setup, wait
	and teardown around `driver.loop`."""
	return driver.loop(
		relation, resolved, prefix, "select", doc=[],
		setup=[
			"\t/* A descriptor past FD_SETSIZE cannot be named, and FD_SET on one",
			"\t * is undefined: refused rather than corrupted. */",
			"\tif (fd < 0 || fd >= FD_SETSIZE) {",
			"\t\trc = SITU_ERR_BOUNDS;",
			"\t\tsys_errno = EBADF;",
			"\t\tgoto done;",
			"\t}",
		],
		wait=[
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
			"\t\t\tsys_errno = errno;",
			"\t\t\tbreak;",
			"\t\t}",
			"\t\tif (n == 0 || !FD_ISSET(fd, &rfds)) {",
			"\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
			"\t\t}",
		],
		teardown=[

		])


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The select driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="select", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run)
