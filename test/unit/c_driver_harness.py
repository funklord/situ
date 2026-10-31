"""The six C drivers held to one harness over a real socket (0033, 26.775).

Every C driver calls the same `situ_drive_hooks_t`, so every one is asked
the same questions here: a reply after retransmissions reaches `on_reply`
with a view whose fields read back; an exchange nobody answers reaches
`on_expired`; a reply to nothing waiting reaches `on_unmatched`; bytes that
are not a reply -- three of them, or seventy thousand, which the drivers'
2048-byte buffer used to cut short in silence -- reach `on_malformed`;
`stop` set from a callback ends the loop; a descriptor the driver cannot
use reaches `on_error` with the system's reason; and `on_done` fires
exactly once on every path out.

The peer is a pthread bounded by `SO_RCVTIMEO` and a stop flag, so nothing
outlives the run and no external process is opened.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from every_schema import ROOT

COMPILER = shutil.which("cc") or shutil.which("gcc")
RUNTIME  = ROOT / "runtime" / "c"
SCHEMA   = ROOT / "example" / "dns" / "dns.situ"

#: `-Wconversion` and `-Wsign-conversion` are the ones a driver over a socket
#: most easily trips, and `-pthread` is the peer.
WARNINGS = ("-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion", "-pthread")

_BODY = r"""
#define _GNU_SOURCE

#include <sys/socket.h>
#include <sys/time.h>
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "situ.h"
#include "dns.h"
#include "dns_drive.h"
#include "dns_@F@.h"

#define CAP 4u
#define QUERY_ID 0x4e4fu

enum { ECHO, SILENT, WRONG_ID, SHORT_FIRST, HUGE_FIRST, ONLY_GARBAGE };

typedef struct {
	uint32_t replies, reply_id, view_id;
	uint32_t expired, unmatched, malformed, errors, done;
	situ_err_t malformed_why, done_rc;
	int error_errno;
	int stop_on_malformed;
	situ_drive_hooks_t *hooks;
} results_t;

static void on_reply(uint32_t id, situ_view_t reply, void *user)
{
	results_t *r = user;
	r->replies += 1u;
	r->reply_id = id;
	r->view_id  = situ_dns_header_id_get(reply);
}

static void on_unmatched(situ_view_t reply, situ_err_t why, void *user)
{
	(void)reply;
	(void)why;
	((results_t *)user)->unmatched += 1u;
}

static void on_malformed(const uint8_t *data, uint32_t len, situ_err_t why,
                         void *user)
{
	results_t *r = user;
	(void)data;
	(void)len;
	r->malformed += 1u;
	r->malformed_why = why;
	if (r->stop_on_malformed != 0) {
		r->hooks->stop = 1;
	}
}

static void on_expired(uint32_t count, void *user)
{
	((results_t *)user)->expired += count;
}

static void on_error(situ_err_t rc, int sys_errno, void *user)
{
	results_t *r = user;
	(void)rc;
	r->errors += 1u;
	r->error_errno = sys_errno;
}

static void on_done(situ_err_t rc, void *user)
{
	results_t *r = user;
	r->done += 1u;
	r->done_rc = rc;
}

typedef struct {
	int fd;
	int mode;
	uint32_t drop_first;
	volatile sig_atomic_t stop;
	uint32_t seen;
} peer_t;

static uint8_t huge[70000];

static void *peer_main(void *arg)
{
	peer_t *p = arg;
	uint8_t buf[2048];
	while (p->stop == 0) {
		const ssize_t got = recv(p->fd, buf, sizeof buf, 0);
		if (got < 0) {
			continue;	/* SO_RCVTIMEO expiry: check stop and retry */
		}
		p->seen += 1u;
		if (p->mode == SILENT || p->seen <= p->drop_first) {
			continue;
		}
		if (p->mode == WRONG_ID) {
			buf[1] = (uint8_t)(buf[1] + 1u);
		}
		if (p->seen == p->drop_first + 1u && p->mode == SHORT_FIRST) {
			(void)send(p->fd, buf, 3u, 0);
		}
		if (p->seen == p->drop_first + 1u && p->mode == HUGE_FIRST) {
			(void)send(p->fd, huge, sizeof huge, 0);
		}
		if (p->mode == ONLY_GARBAGE) {
			(void)send(p->fd, buf, 3u, 0);
			continue;
		}
		(void)send(p->fd, buf, (size_t)got, 0);
	}
	return NULL;
}

static uint32_t now_ms(void)
{
	struct timespec ts;
	(void)clock_gettime(CLOCK_MONOTONIC, &ts);
	const uint64_t ms = (uint64_t)ts.tv_sec * 1000u
	                  + (uint64_t)ts.tv_nsec / 1000000u;
	return (uint32_t)ms;
}

/* One exchange over a fresh socketpair, drive machine and peer. `bad_fd`
 * hands the driver a descriptor it cannot use. Returns the results, and
 * sets *skip where the facility is absent here (a kernel without io_uring
 * is not a broken driver). */
static int run_case(const char *name, int mode, uint32_t drop_first,
                    int bad_fd, int stop_on_malformed, results_t *res,
                    situ_err_t *rc, uint32_t *seen, int *skip)
{
	int sp[2];
	if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		fprintf(stderr, "%s: socketpair failed\n", name);
		return 1;
	}
	int sndbuf = 1 << 20;
	(void)setsockopt(sp[1], SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof sndbuf);
	(void)setsockopt(sp[0], SOL_SOCKET, SO_RCVBUF, &sndbuf, sizeof sndbuf);
	struct timeval tv = { 0, 50 * 1000 };
	(void)setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

	@CTX_DECL@
	if (@CTX_SETUP@ != 0) {
		*skip = 1;
		(void)close(sp[0]);
		(void)close(sp[1]);
		return 0;
	}

	peer_t peer;
	memset(&peer, 0, sizeof peer);
	peer.fd = sp[1];
	peer.mode = mode;
	peer.drop_first = drop_first;
	pthread_t th;
	if (pthread_create(&th, NULL, peer_main, &peer) != 0) {
		fprintf(stderr, "%s: pthread_create failed\n", name);
		return 1;
	}

	uint8_t req[SITU_DNS_HEADER_SIZE_FIXED];
	memset(req, 0, sizeof req);
	situ_msg_t rmsg;
	situ_view_t query;
	situ_msg_init(&rmsg, req, sizeof req);
	if (situ_dns_header_view(&rmsg, 0u, &query) != SITU_OK) {
		fprintf(stderr, "%s: header view failed\n", name);
		return 1;
	}
	situ_dns_header_id_set(query, QUERY_ID);
	situ_dns_header_opcode_set(query, SITU_OPCODE_QUERY);

	situ_drive_reply_to_slot_t slots[CAP];
	situ_conv_reply_to_slot_t keys[CAP];
	situ_drive_reply_to_t drive;
	situ_drive_reply_to_init(&drive, slots, keys, CAP, @IO@, 40u, 2u);
	if (situ_drive_reply_to_send(&drive, query, req, (uint32_t)sizeof req,
	                             0x1234u, now_ms()) != SITU_OK) {
		fprintf(stderr, "%s: send failed\n", name);
		return 1;
	}

	situ_drive_hooks_t hooks;
	memset(&hooks, 0, sizeof hooks);
	hooks.on_reply     = on_reply;
	hooks.on_unmatched = on_unmatched;
	hooks.on_malformed = on_malformed;
	hooks.on_expired   = on_expired;
	hooks.on_error     = on_error;
	hooks.on_done      = on_done;
	hooks.user         = res;
	res->hooks = &hooks;
	res->stop_on_malformed = stop_on_malformed;
	*rc = @RUN@;

	peer.stop = 1;
	(void)pthread_join(th, NULL);
	*seen = peer.seen;
	@CTX_TEARDOWN@
	(void)close(sp[0]);
	(void)close(sp[1]);
	return 0;
}

#define CHECK(cond) do { if (!(cond)) { \
	fprintf(stderr, "%s: %s failed (replies=%u view_id=0x%x expired=%u " \
	        "unmatched=%u malformed=%u errors=%u errno=%d done=%u rc=%d)\n", \
	        name, #cond, r.replies, r.view_id, r.expired, r.unmatched, \
	        r.malformed, r.errors, r.error_errno, r.done, (int)rc); \
	fail = 1; } } while (0)

int main(void)
{
	int fail = 0, skip = 0;
	results_t r;
	situ_err_t rc;
	uint32_t seen;
	const char *name;

	name = "retransmit-then-reply";
	memset(&r, 0, sizeof r);
	if (run_case(name, ECHO, 2u, 0, 0, &r, &rc, &seen, &skip) != 0) return 1;
	if (skip) { printf("SKIP\n"); return 0; }
	CHECK(rc == SITU_OK && r.replies == 1u && r.reply_id == 0x1234u);
	CHECK(r.view_id == QUERY_ID && r.expired == 0u && seen >= 3u);
	CHECK(r.done == 1u && r.done_rc == SITU_OK && r.errors == 0u);

	name = "expire";
	memset(&r, 0, sizeof r);
	if (run_case(name, SILENT, 0u, 0, 0, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc == SITU_OK && r.replies == 0u && r.expired == 1u && seen >= 3u);
	CHECK(r.done == 1u && r.errors == 0u);

	name = "unmatched";
	memset(&r, 0, sizeof r);
	if (run_case(name, WRONG_ID, 0u, 0, 0, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc == SITU_OK && r.replies == 0u && r.unmatched >= 1u);
	CHECK(r.expired == 1u && r.done == 1u);

	name = "short-datagram";
	memset(&r, 0, sizeof r);
	if (run_case(name, SHORT_FIRST, 0u, 0, 0, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc == SITU_OK && r.malformed == 1u && r.replies == 1u);
	CHECK(r.view_id == QUERY_ID && r.done == 1u);

	name = "oversized-datagram";
	memset(&r, 0, sizeof r);
	if (run_case(name, HUGE_FIRST, 0u, 0, 0, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc == SITU_OK && r.malformed == 1u);
	CHECK(r.malformed_why == SITU_ERR_BOUNDS && r.replies == 1u);

	name = "stop";
	memset(&r, 0, sizeof r);
	if (run_case(name, ONLY_GARBAGE, 0u, 0, 1, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc == SITU_OK && r.malformed == 1u && r.replies == 0u);
	CHECK(r.expired == 0u && r.done == 1u && r.errors == 0u);

#if BAD_FD_ERRORS
	name = "bad-fd";
	memset(&r, 0, sizeof r);
	if (run_case(name, SILENT, 0u, 1, 0, &r, &rc, &seen, &skip) != 0) return 1;
	CHECK(rc != SITU_OK && r.errors == 1u && r.error_errno == EBADF);
	CHECK(r.done == 1u && r.done_rc == rc);
#endif

	if (fail == 0) {
		printf("ALL PASS\n");
	}
	return fail;
}
"""

#: How each driver is set up and run, filled into the body above. `bad_fd`
#: is in scope and names the unusable-descriptor case.
_FD = {
	"CTX_DECL":     "situ_@F@_ctx_t ctx;\n\tctx.fd = sp[0];",
	"CTX_SETUP":    "0",
	"CTX_TEARDOWN": "",
	"IO":           "situ_@F@_io(&ctx)",
	"RUN":          "situ_@F@_reply_to_run(bad_fd ? -1 : sp[0], &drive, &hooks@EXTRA@)",
}
_URING = {
	"CTX_DECL":     "situ_io_uring_ctx_t ctx;",
	"CTX_SETUP":    "situ_io_uring_setup(&ctx, bad_fd ? -1 : sp[0])",
	"CTX_TEARDOWN": "situ_io_uring_teardown(&ctx);",
	"IO":           "situ_io_uring_io(&ctx)",
	"RUN":          "situ_io_uring_reply_to_run(&ctx, &drive, &hooks)",
}

#: The drivers that can see an unusable descriptor. poll(2) and ppoll(2)
#: are specified to ignore a negative one, so for them it is a quiet
#: socket and the exchange expires instead.
BAD_FD_ERRORS = {"epoll": 1, "select": 1, "blocking": 1, "io_uring": 1,
                 "poll": 0, "ppoll": 0}


def harness(facility: str, extra: str = "") -> str:
	"""The C harness for one driver; `extra` closes the run call -- ppoll's
	signal mask."""
	fill = _URING if facility == "io_uring" else _FD
	text = _BODY
	for key, value in fill.items():
		text = text.replace(f"@{key}@", value)
	return text.replace("@EXTRA@", extra).replace("@F@", facility)


def run(tmp_path: Path, facility: str, extra: str = "") -> str:
	"""Generate the driver, build the harness against it and run it;
	returns its stdout, which ends `ALL PASS` or is `SKIP`."""
	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--layer", "drive", "--driver", facility,
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	source = tmp_path / "harness.c"
	source.write_text(harness(facility, extra), encoding="ascii")
	assert COMPILER is not None
	binary = tmp_path / "harness"
	compiled = subprocess.run(
		[COMPILER, *WARNINGS, f"-DBAD_FD_ERRORS={BAD_FD_ERRORS[facility]}",
		 f"-I{gen}", f"-I{RUNTIME}", str(source),
		 str(gen / "dns.c"), str(gen / "dns_relate.c"),
		 str(gen / f"dns_{facility}.c"), str(RUNTIME / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=60)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	return ran.stdout
