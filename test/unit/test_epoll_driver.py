"""The epoll driver, held to a real socket (0033).

The `--driver epoll` artifact is the shipped event loop for rung 6: it owns
the fd, the clock and the timer arithmetic, and pumps the `--layer drive`
state machine that owns everything else. A transcript test proves the state
machine; this one proves the driver, which only a real descriptor can. It
generates the driver, drives it over an `AF_UNIX` datagram socketpair, and
asserts the two behaviours the loop exists for: it retransmits an unanswered
query and then correlates the reply, and it expires an exchange once the
retry budget is spent -- reporting each exactly once.

The peer is a pthread bounded by `SO_RCVTIMEO` and a stop flag, so the test
opens no external process and the peer always terminates. epoll(7) is Linux,
so the socket test skips elsewhere; the refusal tests are pure CLI and run
anywhere.
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

#: The exact flags the harness was verified under. `-Wconversion` and
#: `-Wsign-conversion` are the ones a driver over a socket most easily trips,
#: and `-pthread` is the peer.
WARNINGS = ("-std=c11", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion", "-pthread")

#: The driver under test, over a real socketpair. Two cases run back to back,
#: each with a fresh socket, drive machine and peer thread. Timeout is 40 ms
#: and the budget is 2 retries, so both exchanges finish in well under a
#: second while still crossing the timer three times.
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
#include "dns_epoll.h"

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
			continue;	/* SO_RCVTIMEO expiry: check stop and retry */
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
                    uint32_t exp_min_datagrams)
{
	int sp[2];
	if (socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		fprintf(stderr, "%s: socketpair failed\n", name);
		return 1;
	}

	struct timeval tv = { 0, 50 * 1000 };
	(void)setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

	peer_t peer;
	memset(&peer, 0, sizeof peer);
	peer.fd = sp[1];
	peer.drop_first = drop_first;
	peer.do_echo = do_echo;

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
		peer.stop = 1;
		(void)pthread_join(th, NULL);
		return 1;
	}
	situ_dns_header_id_set(query, 0x4e4fu);
	situ_dns_header_opcode_set(query, SITU_OPCODE_QUERY);

	situ_drive_reply_to_slot_t slots[CAP];
	situ_conv_reply_to_slot_t keys[CAP];
	situ_epoll_ctx_t ctx;
	ctx.fd = sp[0];
	situ_drive_reply_to_t drive;
	situ_drive_reply_to_init(&drive, slots, keys, CAP,
	                         situ_epoll_io(&ctx), 40u, 2u);

	const uint32_t handle = 0x1234u;
	const situ_err_t serr = situ_drive_reply_to_send(&drive, query, req,
	                                                  (uint32_t)sizeof req,
	                                                  handle, now_ms());
	if (serr != SITU_OK) {
		fprintf(stderr, "%s: send failed (%d)\n", name, (int)serr);
		peer.stop = 1;
		(void)pthread_join(th, NULL);
		return 1;
	}

	results_t res;
	memset(&res, 0, sizeof res);
	const situ_err_t rc = situ_epoll_reply_to_run(sp[0], &drive,
	                                               on_reply, on_expired, &res);

	peer.stop = 1;
	(void)pthread_join(th, NULL);
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
	fail |= run_case("retransmit-then-complete", 2u, 1, 1u, 0u, 3u);
	fail |= run_case("expire", 0u, 0, 0u, 1u, 3u);

	if (fail == 0) {
		printf("ALL PASS\n");
	}
	return fail;
}
"""


@pytest.mark.skipif(COMPILER is None or sys.platform != "linux",
                    reason="needs a C compiler and epoll(7), which is Linux")
def test_epoll_driver_retransmits_and_expires(tmp_path: Path) -> None:
	"""Generate the epoll driver, drive it over a real socket, and prove
	both loop behaviours: a retransmit that then correlates, and an expiry
	after the retry budget."""
	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--layer", "drive", "--driver", "epoll",
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
		 str(gen / "dns_epoll.c"), str(RUNTIME / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=30)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "ALL PASS" in ran.stdout, ran.stdout


def test_epoll_is_refused_on_python(tmp_path: Path) -> None:
	"""A driver crosses the backend axis and epoll on python is a cell the
	compiler declines. The refusal names both the driver and the target."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "python", "--layer", "drive", "--driver", "epoll",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "epoll" in refused.stderr
	assert "python" in refused.stderr


def test_epoll_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a file over the drive layer rather than pulling it in,
	so it must be asked for alongside `--layer drive`. The refusal names both
	the driver and the layer it needs."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--driver", "epoll",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "epoll" in refused.stderr
	assert "drive" in refused.stderr
