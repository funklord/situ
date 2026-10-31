"""The asyncio driver, held to a real event loop (0033).

`--driver asyncio` is the second host runtime and the first Python driver.
Like Qt's, it does not own the loop -- the application's is already running,
and a driver that called `run_until_complete` would be taking it over. Unlike
Qt's, the artifact is a coroutine, so the outcome is what `await` yields
rather than a completion callback: a coroutine is the runtime's own way of
saying "this finishes later", which C++ had no equivalent of.

This runs the generated coroutine on a real event loop over an AF_UNIX
datagram socketpair and asks for every hook it calls -- a reply read
through its view, an expiry, an unmatched reply, a short and an oversized
datagram, `stop` from a hook, and `on_done` exactly once -- the cases
`c_driver_harness` puts to the six C drivers (26.775). The peer is a thread bounded by a
socket timeout and a stop flag, so no external process is opened and it
always terminates.

Needs no toolchain beyond the interpreter running the suite: asyncio is the
standard library, which is most of why this driver is worth having.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from every_schema import ROOT

SCHEMA  = ROOT / "example" / "dns" / "dns.situ"
RUNTIME = ROOT / "runtime" / "python"

#: The exchange under test, driven fast enough to finish in well under a
#: second while still crossing the timer three times: 40 ms and two retries,
#: overriding the schema's own 5000/2 the way a deployment may.
HARNESS = r'''
import asyncio
import socket
import sys
import threading

from situ_runtime import BoundsError, Message
from dns import dns_header
from dns_drive import reply_to_driver
from dns_asyncio import reply_to_hooks, run_reply_to, submit_to, _now_ms

ECHO, SILENT, WRONG_ID, SHORT_FIRST, HUGE_FIRST, ONLY_GARBAGE = range(6)


def peer(sock, mode, drop_first, state, stop):
	# The same peer `c_driver_harness` runs: the timeout is what lets it
	# see the stop flag.
	sock.settimeout(0.05)
	while not stop.is_set():
		try:
			data = bytearray(sock.recv(65535))
		except (socket.timeout, OSError):
			continue
		state["seen"] += 1
		if mode == SILENT or state["seen"] <= drop_first:
			continue
		first = state["seen"] == drop_first + 1
		try:
			if mode == WRONG_ID:
				data[1] = (data[1] + 1) & 0xFF
			if first and mode == SHORT_FIRST:
				sock.send(bytes(data[:3]))
			if first and mode == HUGE_FIRST:
				sock.send(bytes(70000))
			if mode == ONLY_GARBAGE:
				sock.send(bytes(data[:3]))
				continue
			sock.send(bytes(data))
		except OSError:
			pass


class recorder(reply_to_hooks):
	def __init__(self, stop_on_malformed):
		self.replies, self.view_ids, self.expired = [], [], 0
		self.unmatched = self.malformed = self.errors = self.done = 0
		self.malformed_why = None
		self.stop_on_malformed = stop_on_malformed

	def on_reply(self, id, reply):
		self.replies.append(id)
		self.view_ids.append(reply.id)

	def on_unmatched(self, reply, why):
		self.unmatched += 1

	def on_malformed(self, data, why):
		self.malformed += 1
		self.malformed_why = why
		if self.stop_on_malformed:
			self.stop = True

	def on_expired(self, count):
		self.expired += count

	def on_error(self, error):
		self.errors += 1

	def on_done(self, error):
		self.done += 1


async def case(name, mode, drop_first, stop_on_malformed=False):
	a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
	b.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
	a.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
	state, stop = {"seen": 0}, threading.Event()
	th = threading.Thread(target=peer, args=(b, mode, drop_first, state, stop))
	th.start()

	req = bytes([0x4E, 0x4F] + [0] * 10)
	query = dns_header.at(Message(bytearray(req)), 0)
	drive = reply_to_driver(submit_to(a), 4, 40, 2)
	drive.send(query, req, 0x1234, _now_ms())

	hooks = recorder(stop_on_malformed)
	timed_out = False
	try:
		# A watchdog, so a coroutine that never finishes fails rather than
		# hangs the suite.
		await asyncio.wait_for(run_reply_to(a, drive, hooks), 5.0)
	except asyncio.TimeoutError:
		timed_out = True
	stop.set()
	th.join()
	a.close()
	b.close()
	return hooks, state["seen"], timed_out


def check(name, cond, hooks):
	if not cond:
		print("FAIL", name, vars(hooks))
	return 0 if cond else 1


async def main():
	bad = 0
	h, seen, late = await case("retransmit", ECHO, 2)
	bad |= check("retransmit", not late and h.replies == [0x1234]
	             and h.view_ids == [0x4E4F] and h.expired == 0 and seen >= 3
	             and h.done == 1 and h.errors == 0, h)
	h, seen, late = await case("expire", SILENT, 0)
	bad |= check("expire", not late and not h.replies and h.expired == 1
	             and h.done == 1, h)
	h, seen, late = await case("unmatched", WRONG_ID, 0)
	bad |= check("unmatched", not late and h.unmatched >= 1
	             and h.expired == 1 and h.done == 1, h)
	h, seen, late = await case("short", SHORT_FIRST, 0)
	bad |= check("short", not late and h.malformed == 1
	             and h.replies == [0x1234], h)
	h, seen, late = await case("oversized", HUGE_FIRST, 0)
	bad |= check("oversized", not late and h.malformed == 1
	             and isinstance(h.malformed_why, BoundsError)
	             and h.replies == [0x1234], h)
	h, seen, late = await case("stop", ONLY_GARBAGE, 0, True)
	bad |= check("stop", not late and h.malformed == 1 and not h.replies
	             and h.expired == 0 and h.done == 1, h)
	if not bad:
		print("ALL PASS")
	return bad


sys.exit(asyncio.run(main()))
'''


def _generate(tmp_path: Path) -> Path:
	"""Generate the drive layer and its asyncio driver, with the runtime
	beside them so the module imports as a caller's would."""
	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "python", "--layer", "drive", "--driver", "asyncio",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert (gen / "dns_asyncio.py").exists(), built.stderr

	(gen / "situ_runtime.py").write_text(
		(RUNTIME / "situ_runtime.py").read_text(encoding="ascii"),
		encoding="ascii")
	return gen


def test_asyncio_driver_calls_every_hook(tmp_path: Path) -> None:
	"""Drive the generated coroutine on a real event loop and ask for every
	hook."""
	gen = _generate(tmp_path)
	(gen / "harness.py").write_text(HARNESS, encoding="ascii")

	ran = subprocess.run([sys.executable, "harness.py"], cwd=gen,
	                     capture_output=True, text=True, timeout=120)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "ALL PASS" in ran.stdout, ran.stdout


def test_the_generated_driver_imports_cleanly(tmp_path: Path) -> None:
	"""A generated module nobody can import is one nothing else here would
	catch: the behavioural test above imports it, but only where the whole
	exchange runs, and an import error there reads as a driver fault."""
	gen = _generate(tmp_path)
	imported = subprocess.run(
		[sys.executable, "-c", "import dns_asyncio; print(dns_asyncio.__doc__)"],
		cwd=gen, capture_output=True, text=True, timeout=60)
	assert imported.returncode == 0, imported.stderr
	assert "asyncio driver for rung 6" in imported.stdout


def test_asyncio_is_refused_on_c(tmp_path: Path) -> None:
	"""A host runtime is available for its own backend and no other. The
	refusal names both, as it does for every unavailable pair."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "c", "--layer", "drive", "--driver", "asyncio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "asyncio" in refused.stderr
	assert "python" in refused.stderr and "--target is c" in refused.stderr


def test_asyncio_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a module over the drive layer rather than pulling it
	in, and names the Python suffix rather than C's."""
	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "python", "--driver", "asyncio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "asyncio" in refused.stderr and "drive" in refused.stderr
	assert "_asyncio.py" in refused.stderr
