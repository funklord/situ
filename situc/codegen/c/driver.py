"""Shared scaffolding for the rung-6 driver artifacts (decision 0033).

A driver adds `<name>_<facility>.c` and its header over `--layer drive` and
pumps the state machine through the `situ_io_t` submit vtable, owning the
socket, the clock and the timer arithmetic. The readiness and no-multiplexer
drivers -- `epoll`, `poll`, `blocking` -- differ only in how the loop waits
for the deadline `step` returns; everything around that loop is identical, so
it lives here and each driver module supplies just its `_run` plus the few
strings that name it.

`io_uring` is completion rather than readiness -- its context carries a ring,
not a bare fd, and it has a setup/teardown pair -- so it uses only
`reply_view` from here and carries its own header, submit and source. That
split is deliberate: this module holds what the fd-based drivers genuinely
share, not a lowest common denominator forced over the one that is different.
"""

from __future__ import annotations

from typing import Callable

from situc import ast
from situc.codegen.c.drive import driven
from situc.codegen.c.names import ident, macro
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["reply_view", "submit_and_clock", "header", "source", "generate",
           "HOOKS_TYPE", "DATAGRAM_MAX", "call", "deliver", "loop"]

#: A driver's `_run`: the event loop for one relation, the one thing that
#: differs between facilities.
RunFn = Callable[[ast.Relation, ResolvedSchema, str], "list[str]"]

#: The largest datagram a driver receives whole. Not the reply struct's
#: size: that can describe only the front of a datagram -- `dns_header` is
#: twelve bytes of a DNS response -- so it bounds nothing here. 65535 is the
#: most a UDP datagram carries; one longer than the buffer is reported
#: through `on_malformed` rather than read cut short. The buffer was 2048
#: bytes in every driver, so a 4096-byte EDNS response was parsed truncated
#: and nothing said so (26.775).
DATAGRAM_MAX = 65536

#: What a C driver calls, in one struct shared by all six (26.775). The same
#: names, in the same order, are the C++, Python and Rust drivers' hooks.
#: Guarded, because it is spelled `situ_` like `situ_io_t` and two driver
#: headers may meet in one translation unit.
HOOKS_TYPE = [
	"#ifndef SITU_DRIVE_HOOKS_T",
	"#define SITU_DRIVE_HOOKS_T",
	"/* What the loop tells you, every member optional (NULL is ignored).",
	" *",
	" *   on_reply      a reply answered the exchange `id` names. `reply` is a",
	" *                 view of the received datagram: its bytes are the",
	" *                 driver's and valid only for the call, so copy what",
	" *                 you keep.",
	" *   on_unmatched  a reply answered nothing waiting -- a duplicate, one",
	" *                 after its exchange expired, or one nobody asked for.",
	" *                 `why` is what the drive machine's `on_message` said.",
	" *   on_malformed  a datagram that is not a reply: too short for the",
	" *                 reply's view, or longer than the driver receives",
	" *                 whole. `why` is the view's refusal, or BOUNDS.",
	" *   on_expired    `count` exchanges ran out of retries.",
	" *   on_error      the loop is ending on a failure: `rc` as `run` will",
	" *                 return it, and `sys_errno` where the system said why",
	" *                 (zero where it did not).",
	" *   on_done       the loop is ending, with what `run` returns. Called",
	" *                 exactly once, on every path out.",
	" *",
	" * `user` is passed to each. Set `stop` non-zero -- from a callback, or",
	" * from a signal handler, whose interrupted wait ends the turn -- and",
	" * the loop returns SITU_OK at the top of its next turn, leaving what",
	" * is still in flight in the drive machine for a later `run`. */",
	"typedef struct situ_drive_hooks {",
	"\tvoid (*on_reply)(uint32_t id, situ_view_t reply, void *user);",
	"\tvoid (*on_unmatched)(situ_view_t reply, situ_err_t why, void *user);",
	"\tvoid (*on_malformed)(const uint8_t *data, uint32_t len, situ_err_t why,",
	"\t                     void *user);",
	"\tvoid (*on_expired)(uint32_t count, void *user);",
	"\tvoid (*on_error)(situ_err_t rc, int sys_errno, void *user);",
	"\tvoid (*on_done)(situ_err_t rc, void *user);",
	"\tvoid *user;",
	"\tvolatile int stop;",
	"} situ_drive_hooks_t;",
	"#endif",
	"",
]


def call(name: str, args: str, indent: str) -> list[str]:
	"""One hook, called where the caller set it."""
	return [f"{indent}if (hooks != NULL && hooks->{name} != NULL) {{",
	        f"{indent}\thooks->{name}({args}{', ' if args else ''}hooks->user);",
	        f"{indent}}}"]


def deliver(acquire: str, on_msg: str, buf: str, got: str,
		indent: str) -> list[str]:
	"""Hand one received datagram to the drive machine, and every outcome
	to a hook: a reply, a reply nobody is waiting for, or bytes that are
	not one. Dropping the last two silently was the drivers' only answer
	before 26.775, and the reply itself was never passed on at all."""
	return [
		f"{indent}situ_msg_t  msg;",
		f"{indent}situ_view_t reply;",
		f"{indent}uint32_t    id = 0u;",
		f"{indent}situ_msg_init(&msg, {buf}, (uint32_t){got});",
		f"{indent}const situ_err_t viewed = {acquire};",
		f"{indent}if (viewed != SITU_OK) {{",
		*call("on_malformed", f"{buf}, (uint32_t){got}, viewed", indent + "\t"),
		f"{indent}}} else {{",
		f"{indent}\tconst situ_err_t matched = {on_msg}(drive, reply, &id);",
		f"{indent}\tif (matched == SITU_OK) {{",
		*call("on_reply", "id, reply", indent + "\t\t"),
		f"{indent}\t}} else {{",
		*call("on_unmatched", "reply, matched", indent + "\t\t"),
		f"{indent}\t}}",
		f"{indent}}}",
	]


def loop(relation: ast.Relation, resolved: ResolvedSchema, prefix: str,
		facility: str, *, doc: list[str], setup: list[str],
		wait: list[str], teardown: list[str], params: str = "",
		floor: int = 0) -> list[str]:
	"""The whole run function of an fd-based driver, around its wait.

	`setup` runs once and may `goto done` with `rc` and `sys_errno` set;
	`wait` runs each turn with `diff` the milliseconds to the next deadline,
	and either `continue`s (timed out, interrupted), `break`s with `rc` and
	`sys_errno` set, or falls through when the fd is readable; `teardown`
	releases what `setup` took. Everything else -- the stop check, `step`,
	the receive, the delivery and the one exit that calls `on_error` and
	`on_done` -- is here, so the six agree by construction (26.775).
	"""
	drive = ident(prefix, "drive", relation.name)
	run   = ident(prefix, facility, relation.name, "run")
	view_fn, fixed = reply_view(relation, resolved, prefix)
	acquire = (f"{view_fn}(&msg, 0u, &reply)" if fixed
	           else f"{view_fn}(&msg, 0u, (uint32_t)got, &reply)")
	jumps = any("goto done" in line for line in setup)
	return [
		f"/* Drive `{relation.name}` until nothing is outstanding, `stop` is",
		" * set, or the loop fails. Every outcome reaches a hook in `hooks`",
		" * (which may be NULL); see `situ_drive_hooks_t`. Returns SITU_OK",
		" * when `step` answers SITU_ERR_TRUNCATED -- an empty in-flight set,",
		" * which is completion -- or when stopped.",
		*doc,
		" */",
		f"situ_err_t {run}(int fd, {drive}_t *drive,",
		f"                 situ_drive_hooks_t *hooks{params})",
		"{",
		"\tsitu_err_t rc = SITU_OK;",
		"\tint sys_errno = 0;",
		*setup,
		"\tfor (;;) {",
		"\t\tif (hooks != NULL && hooks->stop != 0) {",
		"\t\t\tbreak;",
		"\t\t}",
		f"\t\tconst uint32_t now = {ident(prefix, facility, 'now_ms')}();",
		"\t\tuint32_t next_ms = 0u;",
		"\t\tuint32_t expired = 0u;",
		"",
		"\t\t/* Retransmit what is due, expire what is spent, and learn the",
		"\t\t * earliest remaining deadline. TRUNCATED is an empty in-flight",
		"\t\t * set: no deadline to wait on and the exchange is done. */",
		f"\t\tconst situ_err_t stepped = {drive}_step(drive, now, &next_ms,"
		" &expired);",
		"\t\tif (expired != 0u) {",
		*call("on_expired", "expired", "\t\t\t"),
		"\t\t}",
		"\t\tif (stepped == SITU_ERR_TRUNCATED) {",
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif (stepped != SITU_OK) {",
		"\t\t\trc = stepped;",
		"\t\t\tbreak;",
		"\t\t}",
		"",
		"\t\t/* Milliseconds to the next deadline, wrap-safe and floored. */",
		"\t\tint32_t diff = (int32_t)(next_ms - now);",
		f"\t\tif (diff < {floor}) {{",
		f"\t\t\tdiff = {floor};",
		"\t\t}",
		*wait,
		"",
		"\t\t/* One datagram is one message, received whole or reported:",
		"\t\t * MSG_TRUNC in `msg_flags` is POSIX's word for one longer than",
		"\t\t * the buffer, which `recv` would have cut short in silence. */",
		f"\t\tuint8_t buf[{DATAGRAM_MAX}];",
		"\t\tstruct iovec iov;",
		"\t\tiov.iov_base = buf;",
		"\t\tiov.iov_len  = sizeof buf;",
		"\t\tstruct msghdr mh;",
		"\t\tmemset(&mh, 0, sizeof mh);",
		"\t\tmh.msg_iov    = &iov;",
		"\t\tmh.msg_iovlen = 1;",
		"\t\tconst ssize_t got = recvmsg(fd, &mh, 0);",
		"\t\tif (got < 0) {",
		"\t\t\tif (errno == EINTR || errno == EAGAIN",
		"\t\t\t                || errno == EWOULDBLOCK) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\tsys_errno = errno;",
		"\t\t\tbreak;",
		"\t\t}",
		"\t\tif ((mh.msg_flags & MSG_TRUNC) != 0) {",
		*call("on_malformed", "buf, (uint32_t)got, SITU_ERR_BOUNDS", "\t\t\t"),
		"\t\t\tcontinue;",
		"\t\t}",
		*deliver(acquire, f"{drive}_on_message", "buf", "got", "\t\t"),
		"\t}",
		*(["done:"] if jumps else []),
		*teardown,
		"\tif (rc != SITU_OK) {",
		*call("on_error", "rc, sys_errno", "\t\t"),
		"\t}",
		*call("on_done", "rc", "\t"),
		"\treturn rc;",
		"}",
		"",
	]


def reply_view(relation: ast.Relation, resolved: ResolvedSchema,
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


def submit_and_clock(prefix: str, facility: str) -> list[str]:
	"""The send-only submit vtable and the clock -- one copy, since neither
	depends on which relation is driven. The submit ctx is a bare `int fd`;
	the completion drivers carry more and write their own."""
	submit = ident(prefix, facility, "submit")
	return [
		"/* Where the bytes go: the submit ctx holds the connected datagram",
		" * fd. A datagram send is all-or-nothing, so a full send buffer",
		" * (EAGAIN) is a dropped datagram the retry budget recovers, and an",
		" * interrupt (EINTR) is retried; only a hard error propagates. */",
		f"static situ_err_t {submit}(void *ctx, const uint8_t *data,"
		" uint32_t len)",
		"{",
		f"\t{ident(prefix, facility, 'ctx')}_t *sock = ctx;",
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
		f"situ_io_t {ident(prefix, facility, 'io')}"
		f"({ident(prefix, facility, 'ctx')}_t *ctx)",
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
		f"static uint32_t {ident(prefix, facility, 'now_ms')}(void)",
		"{",
		"\tstruct timespec ts;",
		"\t(void)clock_gettime(CLOCK_MONOTONIC, &ts);",
		"\tconst uint64_t ms = (uint64_t)ts.tv_sec * 1000u",
		"\t                  + (uint64_t)ts.tv_nsec / 1000000u;",
		"\treturn (uint32_t)ms;",
		"}",
		"",
	]


def header(basename: str, prefix: str,
		ready: list[tuple[ast.Relation, tuple[int, int]]],
		facility: str, doc: list[str],
		run_tail: list[str] | None = None,
		extra_includes: list[str] | None = None) -> str:
	"""The `<name>_<facility>.h`: the `int fd` submit context, the vtable
	accessor, the callback types and a run prototype per relation. `doc` is
	the driver's own comment body, the one part that is not boilerplate.

	`run_tail` is what follows `hooks` in the run prototype -- empty, or
	ppoll's signal mask -- and its `_run` writes the matching definition.
	`extra_includes` are system headers the public types need -- ppoll's
	`sigset_t` needs `<signal.h>`.
	"""
	if run_tail is None:
		run_tail = []
	if extra_includes is None:
		extra_includes = []
	guard = macro(prefix, basename, f"{facility.upper()}_H")
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		*doc,
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		f"#include \"{basename}_drive.h\"",
		*(["", *extra_includes] if extra_includes else []),
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
		"/* The submit context: a connected datagram fd. Build the vtable with",
		" * the accessor below and hand it to the drive machine's `_init`. */",
		"typedef struct {",
		"\tint fd;",
		f"}} {ident(prefix, facility, 'ctx')}_t;",
		"",
		f"situ_io_t {ident(prefix, facility, 'io')}"
		f"({ident(prefix, facility, 'ctx')}_t *ctx);",
		"",
		*HOOKS_TYPE,
	]
	for relation, _policy in ready:
		drive = ident(prefix, "drive", relation.name)
		run   = ident(prefix, facility, relation.name, "run")
		params = "".join(run_tail)
		lines += [
			f"situ_err_t {run}(int fd, {drive}_t *drive,",
			f"                 situ_drive_hooks_t *hooks{params});",
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


def source(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str, ready: list[tuple[ast.Relation, tuple[int, int]]],
		facility: str, doc: list[str], includes: list[str],
		run: RunFn, feature: str = "#define _POSIX_C_SOURCE 200809L") -> str:
	"""The `<name>_<facility>.c`: the driver's comment, its system includes,
	the shared submit-and-clock, then its own `_run` per relation.

	`feature` is the feature-test macro, before any include. The default
	exposes POSIX.1-2008 (`clock_gettime`, `SO_RCVTIMEO`); ppoll needs
	`_GNU_SOURCE`, which glibc gates `ppoll` behind and which is a superset.
	"""
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		*doc,
		" */",
		feature,
		"",
		f"#include \"{basename}_{facility}.h\"",
		"",
		*includes,
		# What `loop` itself uses, whatever the facility includes.
		*[line for line in ("#include <sys/socket.h>", "#include <sys/uio.h>",
		                    "#include <errno.h>", "#include <string.h>")
		  if line not in includes],
		"",
		*submit_and_clock(prefix, facility),
	]
	for relation, _policy in ready:
		lines.extend(run(relation, resolved, prefix))
	return "\n".join(lines).rstrip("\n") + "\n"


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str, *, facility: str, header_doc: list[str],
		source_doc: list[str], includes: list[str], run: RunFn,
		run_tail: list[str] | None = None,
		extra_includes: list[str] | None = None,
		feature: str = "#define _POSIX_C_SOURCE 200809L") -> dict[str, str]:
	"""An fd-based driver's header and source, or nothing where no exchange
	states a policy -- the same `driven()` gate the drive layer uses."""
	ready = driven(schema, resolved)
	if not ready:
		return {}
	return {
		f"{basename}_{facility}.h": header(basename, prefix, ready, facility,
		                                   header_doc, run_tail,
		                                   extra_includes),
		f"{basename}_{facility}.c": source(schema, resolved, basename, prefix,
		                                   ready, facility, source_doc,
		                                   includes, run, feature),
	}
