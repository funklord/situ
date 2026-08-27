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
from situc.codegen.c.names import ident
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
_RUN_TAIL = [
	"                 void *user,",
	"                 const sigset_t *sigmask);",
]

_EXTRA_INCLUDES = ["#include <signal.h>"]


def _run(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The event loop for one driven relation."""
	drive   = ident(prefix, "drive", relation.name)
	run     = ident(prefix, "ppoll", relation.name, "run")
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
		" * may be NULL. `sigmask` is applied atomically for each wait and may",
		" * be NULL for no change. The loop returns when nothing is outstanding",
		" * -- `step` answers SITU_ERR_TRUNCATED, which is completion. */",
		f"situ_err_t {run}(int fd, {drive}_t *drive,",
		f"                 {ident(prefix, 'ppoll', 'reply')}_fn on_reply,",
		f"                 {ident(prefix, 'ppoll', 'expired')}_fn on_expired,",
		"                 void *user,",
		"                 const sigset_t *sigmask)",
		"{",
		"\tsitu_err_t rc = SITU_OK;",
		"\tfor (;;) {",
		f"\t\tconst uint32_t now = {ident(prefix, 'ppoll', 'now_ms')}();",
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
		"\t\t * `step` retransmits on the next turn. ppoll takes a timespec,",
		"\t\t * so the millisecond difference is split into it. */",
		"\t\tint32_t diff = (int32_t)(next_ms - now);",
		"\t\tif (diff < 0) {",
		"\t\t\tdiff = 0;",
		"\t\t}",
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
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif (n == 0) {",
		"\t\t\tcontinue;\t/* timed out: `step` retransmits next turn */",
		"\t\t}",
		"",
		"\t\tif ((pfd.revents & POLLIN) != 0) {",
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
	"""The ppoll driver's header and source, or nothing where no exchange
	states a policy."""
	return driver.generate(schema, resolved, basename, prefix,
	                       facility="ppoll", header_doc=_HEADER_DOC,
	                       source_doc=_SOURCE_DOC, includes=_INCLUDES, run=_run,
	                       run_tail=_RUN_TAIL, extra_includes=_EXTRA_INCLUDES,
	                       feature="#define _GNU_SOURCE")
