"""The `io_uring` driver: a Linux completion event loop over the rung-6 machine.

Decision 0033. This is the driver the completion-shaped vtable was built for:
proving the shape rather than assuming it. It is an additive artifact --
`--driver io_uring` adds `<name>_io_uring.c` (and its header) and changes
nothing else -- gated on the same `driven()` test the drive layer uses.

RAW, NOT liburing. The generated code speaks io_uring through `<linux/io_uring.h>`
and the two syscalls directly -- `io_uring_setup` and `io_uring_enter` -- with
the SQ/CQ rings `mmap`ed. So it depends only on the kernel uapi header, not on
`liburing` being installed, which is what lets it compile and run anywhere the
kernel supports io_uring (5.1+, EXT_ARG timeouts 5.11+). Registered buffers
are a latency optimization this build deliberately does not take; ordinary
buffers keep the code the shape a reader already knows.

THE COMPLETION MAPPING. The state machine calls `submit` synchronously to
send: that prepares an `IORING_OP_SEND` sqe and enters to submit it,
fire-and-forget -- its completion is reaped and discarded, a send the retry
budget will resend if it was lost. The loop keeps exactly one `IORING_OP_RECV`
in flight and waits for a completion with a timeout equal to `step`'s next
deadline (`io_uring_enter` with `IORING_ENTER_EXT_ARG` carrying a
`__kernel_timespec`). A recv completion is one message: acquire a view over
the buffer and hand it to `on_message`; then post the next recv.

THE IN-FLIGHT-BUFFER RULE (0033's open question). The drive layer's stance,
stated in `drive.py`, is that an in-flight buffer is not viewed at all: it
holds the *bytes* to retransmit, which are the caller's, and inbound bytes are
the caller's too. This loop keeps that. The recv buffer handed to the kernel
is not viewed until its completion arrives -- only then is a `situ_view_t`
acquired over it -- so 12.3 has nothing to invalidate across the submit and
the question 0033 raised does not arise here. A driver that handed the kernel
a caller's *edit* buffer, or used registered buffers, would have to answer it;
this one does not.
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
	"""The reply message's view accessor, and whether it is the fixed form."""
	reply = relation.params[1]
	struct = resolved.structs[reply.type_name]
	return ident(prefix, reply.type_name, "view"), struct.layout.is_fixed_size


def _shared(prefix: str) -> list[str]:
	"""The ring machinery, the submit vtable and the clock -- one copy, since
	none of it depends on which relation is being driven.

	Fixed boilerplate every instance carries: the two syscall wrappers, the
	ring setup and teardown, the sqe prep, the send-through-submit, and the
	monotonic clock. Only the loop below is per relation.
	"""
	ctx      = f"{ident(prefix, 'io_uring', 'ctx')}_t"
	submit   = ident(prefix, "io_uring", "submit")
	prep     = ident(prefix, "io_uring", "prep")
	enter    = ident(prefix, "io_uring", "enter")
	setup    = ident(prefix, "io_uring", "setup")
	return [
		"/* The two syscalls, wrapped rather than pulled from liburing so the",
		" * generated code depends on the kernel header alone. */",
		f"static int {ident(prefix, 'io_uring', 'setup_sys')}"
		"(unsigned entries, struct io_uring_params *p)",
		"{",
		"\treturn (int)syscall(__NR_io_uring_setup, entries, p);",
		"}",
		"",
		f"static int {enter}(int fd, unsigned to_submit, unsigned min_complete,",
		"                           unsigned flags, void *arg, size_t argsz)",
		"{",
		"\treturn (int)syscall(__NR_io_uring_enter, fd, to_submit,"
		" min_complete,",
		"\t                    flags, arg, argsz);",
		"}",
		"",
		f"/* Set the rings up on `fd`. mmap is not the heap: the ring is memory",
		" * the kernel requires shared, allocated once here and freed by",
		f" * `{ident(prefix, 'io_uring', 'teardown')}`. Nothing on the datagram",
		" * path allocates. */",
		f"situ_err_t {setup}({ctx} *ctx, int fd)",
		"{",
		"\tstruct io_uring_params p;",
		"\tmemset(&p, 0, sizeof p);",
		"\tctx->sock_fd     = fd;",
		"\tctx->recv_posted = 0;",
		f"\tctx->ring_fd = {ident(prefix, 'io_uring', 'setup_sys')}(8u, &p);",
		"\tif (ctx->ring_fd < 0) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tsize_t sring_sz = p.sq_off.array + p.sq_entries * sizeof(unsigned);",
		"\tsize_t cring_sz = p.cq_off.cqes",
		"\t                + p.cq_entries * sizeof(struct io_uring_cqe);",
		"\tif ((p.features & IORING_FEAT_SINGLE_MMAP) != 0u) {",
		"\t\tif (cring_sz > sring_sz) {",
		"\t\t\tsring_sz = cring_sz;",
		"\t\t}",
		"\t\tcring_sz = sring_sz;",
		"\t}",
		"",
		"\tvoid *sq = mmap(0, sring_sz, PROT_READ | PROT_WRITE,",
		"\t                MAP_SHARED | MAP_POPULATE, ctx->ring_fd,",
		"\t                IORING_OFF_SQ_RING);",
		"\tif (sq == MAP_FAILED) {",
		"\t\t(void)close(ctx->ring_fd);",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"\tvoid *cq = sq;",
		"\tif ((p.features & IORING_FEAT_SINGLE_MMAP) == 0u) {",
		"\t\tcq = mmap(0, cring_sz, PROT_READ | PROT_WRITE,",
		"\t\t          MAP_SHARED | MAP_POPULATE, ctx->ring_fd,",
		"\t\t          IORING_OFF_CQ_RING);",
		"\t\tif (cq == MAP_FAILED) {",
		"\t\t\t(void)munmap(sq, sring_sz);",
		"\t\t\t(void)close(ctx->ring_fd);",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		"\t}",
		"\tctx->sqes = mmap(0, p.sq_entries * sizeof(struct io_uring_sqe),",
		"\t                 PROT_READ | PROT_WRITE, MAP_SHARED | MAP_POPULATE,",
		"\t                 ctx->ring_fd, IORING_OFF_SQES);",
		"\tif (ctx->sqes == MAP_FAILED) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"",
		"\tctx->sq_mmap   = sq;",
		"\tctx->sq_bytes  = sring_sz;",
		"\tctx->cq_mmap   = cq;",
		"\tctx->cq_bytes  = cring_sz;",
		"\tctx->sqe_bytes = p.sq_entries * sizeof(struct io_uring_sqe);",
		"\tctx->sring_tail  = (unsigned *)((char *)sq + p.sq_off.tail);",
		"\tctx->sring_mask  = (unsigned *)((char *)sq + p.sq_off.ring_mask);",
		"\tctx->sring_array = (unsigned *)((char *)sq + p.sq_off.array);",
		"\tctx->cring_head  = (unsigned *)((char *)cq + p.cq_off.head);",
		"\tctx->cring_tail  = (unsigned *)((char *)cq + p.cq_off.tail);",
		"\tctx->cring_mask  = (unsigned *)((char *)cq + p.cq_off.ring_mask);",
		"\tctx->cqes = (struct io_uring_cqe *)((char *)cq + p.cq_off.cqes);",
		"\treturn SITU_OK;",
		"}",
		"",
		f"void {ident(prefix, 'io_uring', 'teardown')}({ctx} *ctx)",
		"{",
		"\t(void)munmap(ctx->sqes, ctx->sqe_bytes);",
		"\tif (ctx->cq_mmap != ctx->sq_mmap) {",
		"\t\t(void)munmap(ctx->cq_mmap, ctx->cq_bytes);",
		"\t}",
		"\t(void)munmap(ctx->sq_mmap, ctx->sq_bytes);",
		"\t(void)close(ctx->ring_fd);",
		"}",
		"",
		"/* Publish one sqe to the SQ tail. The tail is released after the entry",
		" * is filled so the kernel never sees a half-written sqe. */",
		f"static void {prep}({ctx} *ctx, uint8_t op, const void *buf,",
		"                          unsigned len, uint64_t ud)",
		"{",
		"\tunsigned tail = atomic_load_explicit(",
		"\t\t(_Atomic unsigned *)ctx->sring_tail, memory_order_acquire);",
		"\tconst unsigned idx = tail & *ctx->sring_mask;",
		"\tstruct io_uring_sqe *s = &ctx->sqes[idx];",
		"\tmemset(s, 0, sizeof *s);",
		"\ts->opcode    = op;",
		"\ts->fd        = ctx->sock_fd;",
		"\ts->addr      = (uint64_t)(uintptr_t)buf;",
		"\ts->len       = len;",
		"\ts->user_data = ud;",
		"\tctx->sring_array[idx] = idx;",
		"\tatomic_store_explicit((_Atomic unsigned *)ctx->sring_tail,",
		"\t                      tail + 1u, memory_order_release);",
		"}",
		"",
		"/* The submit vtable: enqueue a SEND and submit it, fire-and-forget.",
		" * The bytes are the caller's retransmit buffer and are never viewed;",
		" * the send's own completion is reaped and discarded in the loop. */",
		f"static situ_err_t {submit}(void *vctx, const uint8_t *data,"
		" uint32_t len)",
		"{",
		f"\t{ctx} *ctx = vctx;",
		f"\t{prep}(ctx, IORING_OP_SEND, data, len, 1u);\t/* 1 = send */",
		f"\tconst int n = {enter}(ctx->ring_fd, 1u, 0u, 0u, NULL, 0);",
		"\tif (n < 0 && errno != EINTR) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"\treturn SITU_OK;",
		"}",
		"",
		f"situ_io_t {ident(prefix, 'io_uring', 'io')}({ctx} *ctx)",
		"{",
		"\tsitu_io_t io;",
		f"\tio.submit = {submit};",
		"\tio.ctx    = ctx;",
		"\treturn io;",
		"}",
		"",
		"/* The clock, read only here and never by the state machine, wrap-safe",
		" * against the deadline arithmetic in `step`. */",
		f"static uint32_t {ident(prefix, 'io_uring', 'now_ms')}(void)",
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
	"""The completion loop for one driven relation."""
	ctx     = f"{ident(prefix, 'io_uring', 'ctx')}_t"
	drive   = ident(prefix, "drive", relation.name)
	run     = ident(prefix, "io_uring", relation.name, "run")
	prep    = ident(prefix, "io_uring", "prep")
	enter   = ident(prefix, "io_uring", "enter")
	step    = f"{drive}_step"
	on_msg  = f"{drive}_on_message"
	view_fn, fixed = _reply_view(relation, resolved, prefix)

	acquire = (f"{view_fn}(&msg, 0u, &reply)" if fixed
	           else f"{view_fn}(&msg, 0u, (uint32_t)cres, &reply)")

	return [
		f"/* Drive `{relation.name}` until it is answered or expires, over the",
		" * ring `ctx` set up. `on_reply`/`on_expired` may be NULL. The loop",
		" * returns when nothing is outstanding -- `step` answers TRUNCATED. */",
		f"situ_err_t {run}({ctx} *ctx, {drive}_t *drive,",
		f"                 {ident(prefix, 'io_uring', 'reply')}_fn on_reply,",
		f"                 {ident(prefix, 'io_uring', 'expired')}_fn on_expired,",
		"                 void *user)",
		"{",
		"\tsitu_err_t rc = SITU_OK;",
		"\tfor (;;) {",
		"\t\t/* Keep exactly one recv in flight; submit it with the wait. */",
		"\t\tunsigned to_submit = 0u;",
		"\t\tif (ctx->recv_posted == 0) {",
		f"\t\t\t{prep}(ctx, IORING_OP_RECV, ctx->recvbuf,"
		" (unsigned)sizeof ctx->recvbuf, 2u);",
		"\t\t\tctx->recv_posted = 1;",
		"\t\t\tto_submit = 1u;",
		"\t\t}",
		"",
		f"\t\tconst uint32_t now = {ident(prefix, 'io_uring', 'now_ms')}();",
		"\t\tuint32_t next_ms = 0u;",
		"\t\tuint32_t expired = 0u;",
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
		"\t\t/* Wait for a completion up to the next deadline. EXT_ARG carries",
		"\t\t * the timeout as a `__kernel_timespec`; wrap-safe, floored at 0. */",
		"\t\tint32_t diff = (int32_t)(next_ms - now);",
		"\t\tif (diff < 0) {",
		"\t\t\tdiff = 0;",
		"\t\t}",
		"\t\tstruct __kernel_timespec kts;",
		"\t\tkts.tv_sec  = diff / 1000;",
		"\t\tkts.tv_nsec = (long long)(diff % 1000) * 1000000ll;",
		"\t\tstruct io_uring_getevents_arg ga;",
		"\t\tmemset(&ga, 0, sizeof ga);",
		"\t\tga.ts = (uint64_t)(uintptr_t)&kts;",
		"",
		f"\t\tconst int n = {enter}(ctx->ring_fd, to_submit, 1u,",
		"\t\t                      IORING_ENTER_GETEVENTS | IORING_ENTER_EXT_ARG,",
		"\t\t                      &ga, sizeof ga);",
		"\t\tif (n < 0 && errno != ETIME && errno != EINTR) {",
		"\t\t\trc = SITU_ERR_BOUNDS;",
		"\t\t\tbreak;",
		"\t\t}",
		"",
		"\t\t/* Reap every completion. A recv is one message; a send is",
		"\t\t * discarded, its loss covered by the retry budget. */",
		"\t\tunsigned head = atomic_load_explicit(",
		"\t\t\t(_Atomic unsigned *)ctx->cring_head, memory_order_acquire);",
		"\t\tfor (;;) {",
		"\t\t\tconst unsigned tail = atomic_load_explicit(",
		"\t\t\t\t(_Atomic unsigned *)ctx->cring_tail,"
		" memory_order_acquire);",
		"\t\t\tif (head == tail) {",
		"\t\t\t\tbreak;",
		"\t\t\t}",
		"\t\t\tconst struct io_uring_cqe *c =",
		"\t\t\t\t&ctx->cqes[head & *ctx->cring_mask];",
		"\t\t\tconst uint64_t ud   = c->user_data;",
		"\t\t\tconst int32_t  cres = c->res;",
		"\t\t\thead++;",
		"\t\t\tif (ud == 2u) {\t/* a recv */",
		"\t\t\t\tctx->recv_posted = 0;",
		"\t\t\t\tif (cres > 0) {",
		"\t\t\t\t\tsitu_msg_t  msg;",
		"\t\t\t\t\tsitu_view_t reply;",
		"\t\t\t\t\tuint32_t    id = 0u;",
		"\t\t\t\t\tsitu_msg_init(&msg, ctx->recvbuf, (uint32_t)cres);",
		f"\t\t\t\t\tif ({acquire} == SITU_OK",
		f"\t\t\t\t\t                && {on_msg}(drive, reply, &id)"
		" == SITU_OK",
		"\t\t\t\t\t                && on_reply != NULL) {",
		"\t\t\t\t\t\ton_reply(id, user);",
		"\t\t\t\t\t}",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t}",
		"\t\tatomic_store_explicit((_Atomic unsigned *)ctx->cring_head, head,",
		"\t\t                      memory_order_release);",
		"\t}",
		"\treturn rc;",
		"}",
		"",
	]


def _header(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str,
		ready: list[tuple[ast.Relation, tuple[int, int]]]) -> str:
	guard = macro(prefix, basename, "IO_URING_H")
	ctx = f"{ident(prefix, 'io_uring', 'ctx')}_t"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * The io_uring driver: a Linux completion event loop over rung 6",
		" * (decision 0033). Raw io_uring through the kernel header and the two",
		" * syscalls -- no liburing. An additive artifact.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <linux/io_uring.h>",
		"#include <stddef.h>",
		"#include <stdint.h>",
		"",
		f"#include \"{basename}_drive.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
		"/* The ring, set up on a connected datagram fd. Its fields are the",
		" * driver's own -- a caller allocates one on the stack, hands it to",
		" * `_setup`, and passes it to `_io` and the run loop. The recv buffer",
		" * lives here because the kernel writes into it across a submit and it",
		" * is not viewed until the completion (0033). */",
		"typedef struct {",
		"\tint ring_fd;",
		"\tint sock_fd;",
		"\tunsigned *sring_tail;",
		"\tunsigned *sring_mask;",
		"\tunsigned *sring_array;",
		"\tunsigned *cring_head;",
		"\tunsigned *cring_tail;",
		"\tunsigned *cring_mask;",
		"\tstruct io_uring_cqe *cqes;",
		"\tstruct io_uring_sqe *sqes;",
		"\tvoid  *sq_mmap;",
		"\tvoid  *cq_mmap;",
		"\tsize_t sq_bytes;",
		"\tsize_t cq_bytes;",
		"\tsize_t sqe_bytes;",
		"\tint    recv_posted;",
		"\tuint8_t recvbuf[2048];",
		f"}} {ctx};",
		"",
		"/* Set the rings up on `fd`; SITU_ERR_BOUNDS where the kernel refuses",
		" * io_uring. Tear them down when the exchange is done. */",
		f"situ_err_t {ident(prefix, 'io_uring', 'setup')}({ctx} *ctx, int fd);",
		f"void {ident(prefix, 'io_uring', 'teardown')}({ctx} *ctx);",
		"",
		"/* The submit vtable over the ring -- hand it to the drive `_init`. */",
		f"situ_io_t {ident(prefix, 'io_uring', 'io')}({ctx} *ctx);",
		"",
		"/* A correlated reply, and a batch of exchanges that ran out of",
		" * retries. Either callback may be NULL; `user` is threaded through. */",
		f"typedef void (*{ident(prefix, 'io_uring', 'reply')}_fn)"
		"(uint32_t id, void *user);",
		f"typedef void (*{ident(prefix, 'io_uring', 'expired')}_fn)"
		"(uint32_t count, void *user);",
		"",
	]
	for relation, _policy in ready:
		drive = ident(prefix, "drive", relation.name)
		run   = ident(prefix, "io_uring", relation.name, "run")
		lines += [
			f"situ_err_t {run}({ctx} *ctx, {drive}_t *drive,",
			f"                 {ident(prefix, 'io_uring', 'reply')}_fn"
			" on_reply,",
			f"                 {ident(prefix, 'io_uring', 'expired')}_fn"
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
		" * The io_uring completion loop for rung 6 (decision 0033). Linux,",
		" * raw io_uring -- no liburing.",
		" */",
		"#define _GNU_SOURCE",
		"",
		f"#include \"{basename}_io_uring.h\"",
		"",
		"#include <linux/io_uring.h>",
		"#include <sys/syscall.h>",
		"#include <sys/socket.h>",
		"#include <sys/mman.h>",
		"#include <stdatomic.h>",
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
	"""The io_uring driver's header and source, or nothing where no exchange
	states a policy -- the same `driven()` gate the drive layer uses."""
	ready = driven(schema, resolved)
	if not ready:
		return {}
	return {
		f"{basename}_io_uring.h": _header(schema, resolved, basename, prefix,
		                                  ready),
		f"{basename}_io_uring.c": _source(schema, resolved, basename, prefix,
		                                  ready),
	}
