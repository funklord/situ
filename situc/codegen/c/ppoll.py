"""The `ppoll` driver: poll with an atomic signal mask.

Decision 0033. A driver is an additive artifact -- `--driver ppoll` adds
`<name>_ppoll.c` (and its header) and changes nothing else -- that pumps the
`--layer drive` state machine. It is `poll` with the two things `ppoll(2)`
adds: a nanosecond timeout, and -- the reason to reach for it -- a signal
mask applied atomically for the duration of the wait.

The signal mask is what makes ppoll not just poll with ceremony, so this
driver exposes it: the run function takes a `const sigset_t *sigmask`, passed
straight to `ppoll`. A caller that blocks a signal everywhere and hands its
set here has that signal delivered only while the loop waits, with none of
the race the self-pipe trick exists to close -- the handler runs during the
wait, the loop resumes (a signal is an `EINTR`, which `step` retransmits
through), and nothing is lost between checking a flag and sleeping. Passing
NULL asks for no mask change, at which point ppoll is poll with a `timespec`.

The scaffolding it shares with the other fd-based drivers -- the submit
vtable, the clock, most of the header and the `driven()` gate -- lives in
`driver`; what is here is ppoll's loop and the one extra parameter its
prototype carries.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c import driver
from situc.resolve import ResolvedSchema

__all__ = ["generate"]

_HEADER_DOC = [
	" * The ppoll driver: a poll(2) event loop with an atomic signal mask",
	" * over rung 6 (decision 0033). It pumps the `--layer drive` state",
	" * machine, owning the socket, the clock and the timer arithmetic; the",
	" * state machine owns everything else and never reads either. An",
	" * additive artifact -- it adds files rather than changing them.",
]

_SOURCE_DOC = [
	" * The ppoll(2) event loop for rung 6 (decision 0033). Linux and BSD:",
	" * poll with a timespec and a signal mask applied across the wait.",
]

_INCLUDES = [
	"#include <poll.h>",
	"#include <signal.h>",
	"#include <sys/socket.h>",
	"#include <time.h>",
	"#include <errno.h>",
	"#include <string.h>",
	"#include <unistd.h>",
]

#: ppoll's run takes the signal mask after `user`; the header closes its
#: prototype with it and `_run` writes the matching definition.
_RUN_TAIL = [",\n                 const sigset_t *sigmask"]

_EXTRA_INCLUDES = ["#include <signal.h>"]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation: ppoll's setup, wait
	and teardown around `driver.loop`."""
	return driver.loop(
		relation, resolved, prefix, "ppoll", doc=[], params=",\n                 const sigset_t *sigmask",
		setup=[

		],
		wait=[
			"\t\tstruct timespec ts;",
			"\t\tts.tv_sec  = diff / 1000;",
			"\t\tts.tv_nsec = (long)(diff % 1000) * 1000000L;",
			"",
			"\t\t/* One descriptor, rebuilt each turn, and the caller's mask",
			"\t\t * applied only across the wait -- which is the whole of what",
			"\t\t * ppoll has over poll. */",
			"\t\tstruct pollfd pfd;",
			"\t\tpfd.fd      = fd;",
			"\t\tpfd.events  = POLLIN;",
			"\t\tpfd.revents = 0;",
			"\t\tconst int n = ppoll(&pfd, 1, &ts, sigmask);",
			"\t\tif (n < 0) {",
			"\t\t\tif (errno == EINTR) {",
			"\t\t\t\tcontinue;\t/* a signal fired during the wait; retry */",
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
	"""The ppoll driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="ppoll", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run,
	                       run_tail=_RUN_TAIL, extra_includes=_EXTRA_INCLUDES,
	                       feature="#define _GNU_SOURCE")
