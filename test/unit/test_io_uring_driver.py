"""The io_uring driver, held to a real socket (0033).

`--driver io_uring` is the completion-shaped driver the vtable was built for.
It speaks io_uring raw -- the kernel header and the two syscalls, no liburing
-- so the generated code compiles and runs anywhere the kernel supports
io_uring, which is what lets this test drive it. A transcript test proves the
state machine; this one proves the driver, which only a real ring can. It
generates the driver, drives it over an `AF_UNIX` datagram socketpair, and
asserts the two behaviours the loop exists for: it retransmits an unanswered
query and then correlates the reply, and it expires an exchange once the retry
budget is spent -- reporting each exactly once.

The completion mapping is the interesting part: `submit` sends by preparing an
`IORING_OP_SEND` sqe fire-and-forget, and the loop keeps one `IORING_OP_RECV`
in flight and waits for it with the deadline as an EXT_ARG timeout. The recv
buffer is not viewed until its completion, which is how the driver keeps the
drive layer's "in-flight buffer is not viewed" rule (0033).

The peer is a pthread bounded by `SO_RCVTIMEO` and a stop flag, so the test
opens no external process. io_uring is Linux, so the socket test skips
elsewhere and skips too where a kernel offers no io_uring; the refusal tests
are pure CLI and run anywhere.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

COMPILER = shutil.which("cc") or shutil.which("gcc")
RUNTIME  = ROOT / "runtime" / "c"
SCHEMA   = ROOT / "example" / "dns" / "dns.situ"

#: `-pthread` is the peer; the driver itself needs no library -- raw io_uring
#: links against libc alone.
WARNINGS = ("-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion", "-pthread")

#: The driver under test, over a real socketpair. If the kernel offers no
#: io_uring, `situ_io_uring_setup` fails and the harness prints SKIP and exits
#: 0 rather than failing -- a CI without io_uring is not a broken driver.
HARNESS = r"""
#define _POSIX_C_SOURCE 200809L

#include <sys/socket.h>
#include <sys/time.h>
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
#include "dns_io_uring.h"

#define CAP 4u

typedef struct {
	uint32_t reply_count;
	uint32_t reply_id;
	uint32_t expired_count;
} results_t;

static void on_reply(uint32_t id, void *user)
{
	results_t *r = user;
	r->reply_count += 1u;
	r->reply_id = id;
}

static void on_expired(uint32_t count, void *user)
{
	results_t *r = user;
	r->expired_count += count;
}

typedef struct {
	int fd;
	uint32_t drop_first;
	int do_echo;
	volatile sig_atomic_t stop;
	uint32_t seen;
} peer_t;

static void *peer_main(void *arg)
{
	peer_t *p = arg;
	uint8_t buf[2048];
	while (p->stop == 0) {
		const ssize_t got = recv(p->fd, buf, sizeof buf, 0);
		if (got < 0) {
			continue;
		}
		p->seen += 1u;
		if (p->do_echo != 0 && p->seen > p->drop_first) {
			(void)send(p->fd, buf, (size_t)got, 0);
		}
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

static int run_case(const char *name, uint32_t drop_first, int do_echo,
                    uint32_t exp_replies, uint32_t exp_expired,
                    uint32_t exp_min_datagrams, int *skip)
{
	int sp[2];
	if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		fprintf(stderr, "%s: socketpair failed\n", name);
		return 1;
	}

	struct timeval tv = { 0, 50 * 1000 };
	(void)setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

	situ_io_uring_ctx_t ctx;
	if (situ_io_uring_setup(&ctx, sp[0]) != SITU_OK) {
		/* No io_uring here: not a driver fault. */
		*skip = 1;
		(void)close(sp[0]);
		(void)close(sp[1]);
		return 0;
	}

	peer_t peer;
	memset(&peer, 0, sizeof peer);
	peer.fd = sp[1];
	peer.drop_first = drop_first;
	peer.do_echo = do_echo;

	pthread_t th;
	if (pthread_create(&th, NULL, peer_main, &peer) != 0) {
		fprintf(stderr, "%s: pthread_create failed\n", name);
		situ_io_uring_teardown(&ctx);
		return 1;
	}

	uint8_t req[SITU_DNS_HEADER_SIZE_FIXED];
	memset(req, 0, sizeof req);
	situ_msg_t rmsg;
	situ_view_t query;
	situ_msg_init(&rmsg, req, sizeof req);
	if (situ_dns_header_view(&rmsg, 0u, &query) != SITU_OK) {
		fprintf(stderr, "%s: header view failed\n", name);
		peer.stop = 1;
		(void)pthread_join(th, NULL);
		situ_io_uring_teardown(&ctx);
		return 1;
	}
	situ_dns_header_id_set(query, 0x4e4fu);
	situ_dns_header_opcode_set(query, SITU_OPCODE_QUERY);

	situ_drive_reply_to_slot_t slots[CAP];
	situ_conv_reply_to_slot_t keys[CAP];
	situ_drive_reply_to_t drive;
	situ_drive_reply_to_init(&drive, slots, keys, CAP,
	                         situ_io_uring_io(&ctx), 40u, 2u);

	const uint32_t handle = 0x1234u;
	const situ_err_t serr = situ_drive_reply_to_send(&drive, query, req,
	                                                  (uint32_t)sizeof req,
	                                                  handle, now_ms());
	if (serr != SITU_OK) {
		fprintf(stderr, "%s: send failed (%d)\n", name, (int)serr);
		peer.stop = 1;
		(void)pthread_join(th, NULL);
		situ_io_uring_teardown(&ctx);
		return 1;
	}

	results_t res;
	memset(&res, 0, sizeof res);
	const situ_err_t rc = situ_io_uring_reply_to_run(&ctx, &drive,
	                                                 on_reply, on_expired,
	                                                 &res);

	peer.stop = 1;
	(void)pthread_join(th, NULL);
	situ_io_uring_teardown(&ctx);
	(void)close(sp[0]);
	(void)close(sp[1]);

	int fail = 0;
	if (rc != SITU_OK) {
		fprintf(stderr, "%s: run returned %d\n", name, (int)rc);
		fail = 1;
	}
	if (res.reply_count != exp_replies) {
		fprintf(stderr, "%s: replies=%u expected %u\n",
		        name, res.reply_count, exp_replies);
		fail = 1;
	}
	if (exp_replies != 0u && res.reply_id != handle) {
		fprintf(stderr, "%s: reply id=0x%x expected 0x%x\n",
		        name, res.reply_id, handle);
		fail = 1;
	}
	if (res.expired_count != exp_expired) {
		fprintf(stderr, "%s: expired=%u expected %u\n",
		        name, res.expired_count, exp_expired);
		fail = 1;
	}
	if (peer.seen < exp_min_datagrams) {
		fprintf(stderr, "%s: peer saw %u datagrams, expected >= %u\n",
		        name, peer.seen, exp_min_datagrams);
		fail = 1;
	}

	if (fail == 0) {
		printf("PASS %s: replies=%u id=0x%x expired=%u datagrams=%u\n",
		       name, res.reply_count, res.reply_id,
		       res.expired_count, peer.seen);
	}
	return fail;
}

int main(void)
{
	int fail = 0;
	int skip = 0;
	fail |= run_case("retransmit-then-complete", 2u, 1, 1u, 0u, 3u, &skip);
	if (skip == 0) {
		fail |= run_case("expire", 0u, 0, 0u, 1u, 3u, &skip);
	}

	if (skip != 0) {
		printf("SKIP io_uring unavailable\n");
		return 0;
	}
	if (fail == 0) {
		printf("ALL PASS\n");
	}
	return fail;
}
"""


@pytest.mark.skipif(COMPILER is None or sys.platform != "linux",
                    reason="needs a C compiler and io_uring, which is Linux")
def test_io_uring_driver_retransmits_and_expires(tmp_path: Path) -> None:
	"""Generate the io_uring driver, drive it over a real socket, and prove
	both loop behaviours: a retransmit that then correlates, and an expiry
	after the retry budget. Skips cleanly where the kernel offers no
	io_uring."""
	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--layer", "drive", "--driver", "io_uring",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	source = tmp_path / "harness.c"
	source.write_text(HARNESS, encoding="ascii")

	assert COMPILER is not None
	binary = tmp_path / "harness"
	compiled = subprocess.run(
		[COMPILER, *WARNINGS,
		 f"-I{gen}", f"-I{RUNTIME}",
		 str(source),
		 str(gen / "dns.c"), str(gen / "dns_relate.c"),
		 str(gen / "dns_io_uring.c"), str(RUNTIME / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=30)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	if "SKIP" in ran.stdout:
		pytest.skip("the kernel offers no io_uring here")
	assert "ALL PASS" in ran.stdout, ran.stdout


def test_io_uring_is_refused_on_python(tmp_path: Path) -> None:
	"""A driver crosses the backend axis and io_uring on python is nothing at
	all. The refusal names both the driver and the target."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "python", "--layer", "drive", "--driver", "io_uring",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "io_uring" in refused.stderr
	assert "python" in refused.stderr


def test_io_uring_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a file over the drive layer rather than pulling it in,
	so it must be asked for alongside `--layer drive`. The refusal names both
	the driver and the layer it needs."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--driver", "io_uring",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "io_uring" in refused.stderr
	assert "drive" in refused.stderr
