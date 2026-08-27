"""The Qt driver, held to a real Qt event loop (0033).

`--driver qt` is the first driver that is not an OS facility but a host
runtime, and the first that is not C. Qt's event loop belongs to the
application, so the driver does not own one: it installs a QSocketNotifier
and a single-shot QTimer, wires them to the rung-6 state machine, and
returns. This drives the generated code inside a real QCoreApplication over
an AF_UNIX datagram socketpair and asserts the two behaviours the loop
exists for -- it retransmits an unanswered query and then correlates the
reply, and it expires an exchange once the retry budget is spent.

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
 * over an AF_UNIX datagram socketpair. Two cases: the peer drops the first
 * two datagrams then echoes the third (retransmit, then correlate), and the
 * peer never replies (expire after the retry budget). */
#include <QtCore/QCoreApplication>
#include <QtCore/QTimer>

#include <pthread.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <unistd.h>

#include "dns_qt.hpp"

namespace {

struct peer_state {
	int fd;
	unsigned drop_first;
	int do_echo;
	volatile sig_atomic_t stop;
	unsigned seen;
};

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
		if (p->do_echo != 0 && p->seen > p->drop_first) {
			(void)::send(p->fd, buf, static_cast<size_t>(got), 0);
		}
	}
	return nullptr;
}

int run_case(const char *name, unsigned drop_first, int do_echo,
             unsigned want_replies, unsigned want_expired, unsigned want_min)
{
	int sp[2];
	if (::socketpair(AF_UNIX, SOCK_DGRAM, 0, sp) != 0) {
		std::fprintf(stderr, "%s: socketpair failed\n", name);
		return 1;
	}
	struct timeval tv;
	tv.tv_sec = 0;
	tv.tv_usec = 50 * 1000;
	(void)::setsockopt(sp[1], SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);

	peer_state peer;
	std::memset(&peer, 0, sizeof peer);
	peer.fd = sp[1];
	peer.drop_first = drop_first;
	peer.do_echo = do_echo;

	pthread_t th;
	if (::pthread_create(&th, nullptr, peer_main, &peer) != 0) {
		std::fprintf(stderr, "%s: pthread_create failed\n", name);
		return 1;
	}

	/* A 12-byte dns_header. The peer echoes these bytes, so the reply's
	 * id and opcode equal the query's and the exchange correlates. */
	std::uint8_t req[12];
	std::memset(req, 0, sizeof req);
	req[0] = 0x4eu;
	req[1] = 0x4fu;

	::situ::rt::message owner(req, static_cast<std::uint32_t>(sizeof req));
	::situ::dns_header query;
	if (::situ::dns_header::at(owner, 0u, query) != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: could not acquire the query view\n", name);
		peer.stop = 1;
		(void)::pthread_join(th, nullptr);
		return 1;
	}

	::situ::qt_io sink(sp[0]);
	::situ::reply_to_driver::slot store[4];
	::situ::reply_to_driver drive(store, 4u, sink, 40u, 2u);

	const std::uint32_t handle = 0x1234u;
	if (drive.send(query, req, static_cast<std::uint32_t>(sizeof req),
	               handle, ::situ::qt_now_ms()) != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: send failed\n", name);
		peer.stop = 1;
		(void)::pthread_join(th, nullptr);
		return 1;
	}

	unsigned replies = 0u;
	unsigned expiries = 0u;
	std::uint32_t got_id = 0u;
	bool done = false;

	const ::situ::rt::err armed = ::situ::qt_reply_to_run(
		sp[0], drive,
		[&](std::uint32_t id) { replies += 1u; got_id = id; },
		[&](std::uint32_t n) { expiries += n; },
		[&](::situ::rt::err) { done = true; QCoreApplication::quit(); });
	if (armed != ::situ::rt::err::ok) {
		std::fprintf(stderr, "%s: arming failed\n", name);
		peer.stop = 1;
		(void)::pthread_join(th, nullptr);
		return 1;
	}

	/* A watchdog, so a loop that never completes fails rather than hangs. */
	bool timed_out = false;
	QTimer::singleShot(3000, [&]() { timed_out = true;
	                                 QCoreApplication::quit(); });
	QCoreApplication::exec();

	peer.stop = 1;
	(void)::pthread_join(th, nullptr);
	(void)::close(sp[0]);
	(void)::close(sp[1]);

	int fail = 0;
	if (timed_out) {
		std::fprintf(stderr, "%s: watchdog fired; the loop never finished\n",
		             name);
		fail = 1;
	}
	if (!done) {
		std::fprintf(stderr, "%s: on_done never fired\n", name);
		fail = 1;
	}
	if (replies != want_replies) {
		std::fprintf(stderr, "%s: replies=%u want %u\n", name, replies,
		             want_replies);
		fail = 1;
	}
	if (want_replies != 0u && got_id != handle) {
		std::fprintf(stderr, "%s: id=0x%x want 0x%x\n", name, got_id, handle);
		fail = 1;
	}
	if (expiries != want_expired) {
		std::fprintf(stderr, "%s: expired=%u want %u\n", name, expiries,
		             want_expired);
		fail = 1;
	}
	if (peer.seen < want_min) {
		std::fprintf(stderr, "%s: peer saw %u datagrams, want >= %u\n", name,
		             peer.seen, want_min);
		fail = 1;
	}
	if (fail == 0) {
		std::printf("PASS %s: replies=%u id=0x%x expired=%u datagrams=%u\n",
		            name, replies, got_id, expiries, peer.seen);
	}
	return fail;
}

}  /* namespace */

int main(int argc, char **argv)
{
	QCoreApplication app(argc, argv);

	int fail = 0;
	fail |= run_case("retransmit-then-correlate", 2u, 1, 1u, 0u, 3u);
	fail |= run_case("expire", 0u, 0, 0u, 1u, 3u);

	if (fail == 0) {
		std::printf("ALL PASS\n");
	}
	return fail;
}
"""


@pytest.mark.skipif(CXX is None or QT_CFLAGS is None,
                    reason="needs a C++ compiler and Qt6Core")
def test_qt_driver_retransmits_and_expires(tmp_path: Path) -> None:
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
