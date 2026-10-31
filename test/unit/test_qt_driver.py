"""The Qt driver, held to a real Qt event loop (0033).

`--driver qt` is the first driver that is not an OS facility but a host
runtime, and the first that is not C. Qt's event loop belongs to the
application, so the driver does not own one: it installs a QSocketNotifier
and a single-shot QTimer, wires them to the rung-6 state machine, and
returns. This drives the generated code inside a real QCoreApplication over
an AF_UNIX datagram socketpair and asks for every hook the pump calls --
a reply read through its view, an expiry, an unmatched reply, a short and
an oversized datagram, `stop()` from a hook, and `on_done` exactly once --
the cases `c_driver_harness` puts to the six C drivers (26.775).

Qt's own headers do not survive `-Wconversion -Werror`, so they are included
with `-isystem`: situ's generated code is still held to the repository's
strict set, and Qt's is treated as what it is, a third-party system header.

The `slots`-macro hazard these layers had is guarded separately, in
`test_cpp_survives_qt_macros.py`: it belongs to the C++ drive and converse
rungs rather than to this driver, and it needs no Qt to check.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

CXX     = shutil.which("g++") or shutil.which("clang++")
RUNTIME = ROOT / "runtime"
SCHEMA  = ROOT / "example" / "dns" / "dns.situ"


def _qt_flags() -> list[str] | None:
	"""Qt6Core's include flags, with `-I` turned into `-isystem`.

	Qt's headers raise `-Wconversion`/`-Wsign-conversion` by the dozen --
	`qnamespace.h` alone has several -- so under the repository's `-Werror`
	they must be system headers or no Qt program compiles at all. That
	suppresses warnings from Qt and from nothing else: the generated header
	is still compiled under the full set.
	"""
	if shutil.which("pkg-config") is None:
		return None
	found = subprocess.run(["pkg-config", "--cflags", "Qt6Core"],
	                       capture_output=True, text=True)
	if found.returncode != 0:
		return None
	return [word.replace("-I", "-isystem", 1) if word.startswith("-I") else word
	        for word in found.stdout.split()]


def _qt_libs() -> list[str]:
	found = subprocess.run(["pkg-config", "--libs", "Qt6Core"],
	                       capture_output=True, text=True)
	return found.stdout.split() if found.returncode == 0 else []


QT_CFLAGS = _qt_flags()

#: The repository's C++ warning set, as `test_relations.py` compiles the
#: drive layer with. `-fno-rtti`/`-fno-exceptions` are deliberately absent:
#: Qt is built with both, and the drive-layer tests omit them too.
WARNINGS = ("-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
            "-Wsign-conversion", "-Werror", "-fPIC", "-pthread")

HARNESS = r"""
/* Drives the generated `--driver qt` artifact inside a real Qt event loop,
 * over an AF_UNIX datagram socketpair, and asks for every hook the pump
 * calls -- the same cases `c_driver_harness` puts to the six C drivers,
 * since the hooks are the same by name (26.775). */
#include <QtCore/QCoreApplication>
#include <QtCore/QTimer>

#include <pthread.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <memory>
#include <unistd.h>

#include "dns_qt.hpp"

namespace {

enum { ECHO, SILENT, WRONG_ID, SHORT_FIRST, HUGE_FIRST, ONLY_GARBAGE };

struct peer_state {
	int fd;
	int mode;
	unsigned drop_first;
	volatile sig_atomic_t stop;
	unsigned seen;
};

std::uint8_t huge[70000];

void *peer_main(void *arg)
{
	peer_state *p = static_cast<peer_state *>(arg);
	std::uint8_t buf[2048];
	while (p->stop == 0) {
		const ssize_t got = ::recv(p->fd, buf, sizeof buf, 0);
		if (got < 0) {
			continue;	/* SO_RCVTIMEO expiry: re-check the stop flag */
		}
		p->seen += 1u;
		if (p->mode == SILENT || p->seen <= p->drop_first) {
			continue;
		}
		if (p->mode == WRONG_ID) {
			buf[1] = static_cast<std::uint8_t>(buf[1] + 1u);
		}
		if (p->seen == p->drop_first + 1u && p->mode == SHORT_FIRST) {
			(void)::send(p->fd, buf, 3u, 0);
		}
		if (p->seen == p->drop_first + 1u && p->mode == HUGE_FIRST) {
			(void)::send(p->fd, huge, sizeof huge, 0);
		}
		if (p->mode == ONLY_GARBAGE) {
			(void)::send(p->fd, buf, 3u, 0);
			continue;
		}
		(void)::send(p->fd, buf, static_cast<size_t>(got), 0);
	}
	return nullptr;
}

struct results {
	unsigned replies = 0u, expired = 0u, unmatched = 0u, malformed = 0u;
	unsigned errors = 0u, done = 0u, seen = 0u;
	std::uint32_t id = 0u, view_id = 0u;
	::situ::rt::err malformed_why = ::situ::rt::err::ok;
	::situ::rt::err done_rc = ::situ::rt::err::bounds;
	bool timed_out = false;
};

int run_case(const char *name, int mode, unsigned drop_first,
             bool stop_on_malformed, results &r)
{
	int sp[2];
	if (::socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		std::fprintf(stderr, "%s: socketpair failed\n", name);
		return 1;
	}
	int big = 1 << 20;
	(void)::setsockopt(sp[1], SOL_SOCKET, SO_SNDBUF, &big, sizeof big);
	(void)::setsockopt(sp[0], SOL_SOCKET, SO_RCVBUF, &big, sizeof big);
	struct timeval tv;
	tv.tv_sec = 0;
	tv.tv_usec = 50 * 1000;
	(void)::setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

	peer_state peer;
	std::memset(&peer, 0, sizeof peer);
	peer.fd = sp[1];
	peer.mode = mode;
	peer.drop_first = drop_first;
	pthread_t th;
	if (::pthread_create(&th, nullptr, peer_main, &peer) != 0) {
		std::fprintf(stderr, "%s: pthread_create failed\n", name);
		return 1;
	}

	std::uint8_t req[12];
	std::memset(req, 0, sizeof req);
	req[0] = 0x4eu;
	req[1] = 0x4fu;
	::situ::rt::message owner(req, static_cast<std::uint32_t>(sizeof req));
	::situ::dns_header query;
	if (::situ::dns_header::at(owner, 0u, query) != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: could not acquire the query view\n", name);
		return 1;
	}
	::situ::qt_io sink(sp[0]);
	::situ::reply_to_driver::slot store[4];
	::situ::reply_to_driver drive(store, 4u, sink, 40u, 2u);
	if (drive.send(query, req, static_cast<std::uint32_t>(sizeof req),
	               0x1234u, ::situ::qt_now_ms()) != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: send failed\n", name);
		return 1;
	}

	::situ::qt_reply_to_pump *pump = nullptr;
	::situ::qt_reply_to_hooks hooks;
	hooks.on_reply = [&](std::uint32_t id, const ::situ::dns_header &reply) {
		r.replies += 1u;
		r.id = id;
		r.view_id = reply.id();
	};
	hooks.on_unmatched = [&](const ::situ::dns_header &, ::situ::rt::err) {
		r.unmatched += 1u;
	};
	hooks.on_malformed = [&](const std::uint8_t *, std::uint32_t,
	                         ::situ::rt::err why) {
		r.malformed += 1u;
		r.malformed_why = why;
		if (stop_on_malformed && pump != nullptr) {
			pump->stop();
		}
	};
	hooks.on_expired = [&](std::uint32_t n) { r.expired += n; };
	hooks.on_error = [&](::situ::rt::err, int) { r.errors += 1u; };
	hooks.on_done = [&](::situ::rt::err rc) {
		r.done += 1u;
		r.done_rc = rc;
		QCoreApplication::quit();
	};

	/* The pump itself rather than `qt_reply_to_run`, so a hook can reach
	 * `stop()`; the runner is the same pump, heap-allocated. */
	auto owned = std::make_unique<::situ::qt_reply_to_pump>(sp[0], drive,
	                                                        hooks);
	pump = owned.get();
	if (pump->start() != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: arming failed\n", name);
		return 1;
	}
	/* A watchdog, so a loop that never completes fails rather than hangs. */
	QTimer::singleShot(3000, [&]() { r.timed_out = true;
	                                 QCoreApplication::quit(); });
	QCoreApplication::exec();

	peer.stop = 1;
	(void)::pthread_join(th, nullptr);
	r.seen = peer.seen;
	owned.reset();
	(void)::close(sp[0]);
	(void)::close(sp[1]);
	return 0;
}

}  /* namespace */

#define CHECK(cond) do { if (!(cond)) { \
	std::fprintf(stderr, "%s: %s failed (replies=%u view_id=0x%x " \
	             "expired=%u unmatched=%u malformed=%u errors=%u done=%u)\n", \
	             name, #cond, r.replies, r.view_id, r.expired, r.unmatched, \
	             r.malformed, r.errors, r.done); \
	fail = 1; } } while (0)

int main(int argc, char **argv)
{
	QCoreApplication app(argc, argv);
	int fail = 0;
	const char *name;
	{
		name = "retransmit-then-reply";
		results r;
		if (run_case(name, ECHO, 2u, false, r) != 0) return 1;
		CHECK(!r.timed_out && r.replies == 1u && r.id == 0x1234u);
		CHECK(r.view_id == 0x4e4fu && r.expired == 0u && r.seen >= 3u);
		CHECK(r.done == 1u && r.done_rc == ::situ::rt::err::ok);
	}
	{
		name = "expire";
		results r;
		if (run_case(name, SILENT, 0u, false, r) != 0) return 1;
		CHECK(!r.timed_out && r.replies == 0u && r.expired == 1u);
		CHECK(r.done == 1u && r.errors == 0u);
	}
	{
		name = "unmatched";
		results r;
		if (run_case(name, WRONG_ID, 0u, false, r) != 0) return 1;
		CHECK(!r.timed_out && r.unmatched >= 1u && r.expired == 1u);
	}
	{
		name = "short-datagram";
		results r;
		if (run_case(name, SHORT_FIRST, 0u, false, r) != 0) return 1;
		CHECK(!r.timed_out && r.malformed == 1u && r.replies == 1u);
	}
	{
		name = "oversized-datagram";
		results r;
		if (run_case(name, HUGE_FIRST, 0u, false, r) != 0) return 1;
		CHECK(!r.timed_out && r.malformed == 1u && r.replies == 1u);
		CHECK(r.malformed_why == ::situ::rt::err::bounds);
	}
	{
		name = "stop";
		results r;
		if (run_case(name, ONLY_GARBAGE, 0u, true, r) != 0) return 1;
		CHECK(!r.timed_out && r.malformed == 1u && r.replies == 0u);
		CHECK(r.expired == 0u && r.done == 1u);
		CHECK(r.done_rc == ::situ::rt::err::ok);
	}
	if (fail == 0) {
		std::printf("ALL PASS\n");
	}
	return fail;
}
"""


@pytest.mark.skipif(CXX is None or QT_CFLAGS is None,
                    reason="needs a C++ compiler and Qt6Core")
def test_qt_driver_calls_every_hook(tmp_path: Path) -> None:
	"""Generate the Qt driver, drive it inside a real QCoreApplication over a
	socketpair, and prove both behaviours: a retransmit that then correlates,
	and an expiry after the retry budget."""
	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "cpp", "--layer", "drive", "--driver", "qt",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert (gen / "dns_qt.hpp").exists(), built.stderr

	source = tmp_path / "harness.cpp"
	source.write_text(HARNESS, encoding="ascii")

	assert CXX is not None and QT_CFLAGS is not None
	binary = tmp_path / "harness"
	compiled = subprocess.run(
		[CXX, *WARNINGS, *QT_CFLAGS,
		 f"-I{gen}", f"-I{RUNTIME / 'cpp'}", f"-I{RUNTIME / 'c'}",
		 str(source), str(RUNTIME / "c" / "situ.c"),
		 *_qt_libs(), "-o", str(binary)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=120)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "ALL PASS" in ran.stdout, ran.stdout


@pytest.mark.skipif(CXX is None or QT_CFLAGS is None,
                    reason="needs a C++ compiler and Qt6Core")
def test_the_qt_driver_needs_no_moc(tmp_path: Path) -> None:
	"""A consumer must not have to run `moc` over generated code.

	Declaring Q_OBJECT would put a build step inside situ's output, so every
	connection is made to a lambda with a plain QObject as context -- only
	Qt's own already-moc'd classes need a metaobject. The check is that the
	generated header declares none of the macros that would require moc; the
	compile in the test above is the other half, since a type declaring
	Q_OBJECT without moc fails at link with an undefined vtable.
	"""
	gen = tmp_path / "gen"
	subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "cpp", "--layer", "drive", "--driver", "qt",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True, check=True)

	text = (gen / "dns_qt.hpp").read_text(encoding="ascii")
	code = "\n".join(line for line in text.splitlines()
	                 if not line.lstrip().startswith(("*", "/*")))
	for macro in ("Q_OBJECT", "Q_SLOTS", "Q_SIGNALS", "signals:", "slots:"):
		assert macro not in code, f"the Qt driver would need moc: {macro}"


def test_qt_is_refused_on_c(tmp_path: Path) -> None:
	"""The first driver that is not available for C, which is the direction
	the availability rule was written for. The refusal names both."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--layer", "drive", "--driver", "qt",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "qt" in refused.stderr
	assert "cpp" in refused.stderr and "--target is c" in refused.stderr


def test_qt_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a header over the drive layer rather than pulling it
	in, and says so naming the C++ suffix rather than C's."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "cpp", "--driver", "qt",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "qt" in refused.stderr and "drive" in refused.stderr
	assert "_qt.hpp" in refused.stderr
