"""The asyncio driver, held to a real event loop (0033).

`--driver asyncio` is the second host runtime and the first Python driver.
Like Qt's, it does not own the loop -- the application's is already running,
and a driver that called `run_until_complete` would be taking it over. Unlike
Qt's, the artifact is a coroutine, so the outcome is what `await` yields
rather than a completion callback: a coroutine is the runtime's own way of
saying "this finishes later", which C++ had no equivalent of.

This runs the generated coroutine on a real event loop over an AF_UNIX
datagram socketpair and asserts the two behaviours the loop exists for -- it
retransmits an unanswered query and then correlates the reply, and it expires
an exchange once the retry budget is spent. The peer is a thread bounded by a
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

from situ_runtime import Message
from dns import dns_header
from dns_drive import reply_to_driver
from dns_asyncio import run_reply_to, submit_to, _now_ms


def peer(sock, drop_first, echo, state, stop):
	"""Reads datagrams, counts them, and echoes everything after the first
	`drop_first`. The timeout is what lets it see the stop flag."""
	sock.settimeout(0.05)
	while not stop.is_set():
		try:
			data = sock.recv(65535)
		except (socket.timeout, OSError):
			continue
		state["seen"] += 1
		if echo and state["seen"] > drop_first:
			try:
				sock.send(data)
			except OSError:
				pass


async def case(name, drop_first, echo, want_replies, want_expired, want_min):
	a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
	state, stop = {"seen": 0}, threading.Event()
	th = threading.Thread(target=peer, args=(b, drop_first, echo, state, stop))
	th.start()

	# Twelve bytes of dns_header. The peer echoes them, so the reply's id and
	# opcode equal the query's and the exchange correlates.
	req = bytes([0x4E, 0x4F] + [0] * 10)
	query = dns_header.at(Message(bytearray(req)), 0)
	drive = reply_to_driver(submit_to(a), 4, 40, 2)
	drive.send(query, req, 0x1234, _now_ms())

	replies, expiries = [], []
	timed_out = False
	try:
		# A watchdog, so a coroutine that never finishes fails rather than
		# hangs the suite.
		await asyncio.wait_for(
			run_reply_to(a, drive, lambda i: replies.append(i),
			             lambda n: expiries.append(n)), 5.0)
	except asyncio.TimeoutError:
		timed_out = True
	stop.set()
	th.join()
	a.close()
	b.close()

	ok = (not timed_out
	      and len(replies) == want_replies
	      and sum(expiries) == want_expired
	      and state["seen"] >= want_min
	      and (not want_replies or replies[0] == 0x1234))
	print("{} {}: replies={} expired={} datagrams={}{}".format(
		"PASS" if ok else "FAIL", name, replies, sum(expiries),
		state["seen"], " WATCHDOG" if timed_out else ""))
	return 0 if ok else 1


async def main():
	bad = await case("retransmit-then-correlate", 2, True, 1, 0, 3)
	bad |= await case("expire", 0, False, 0, 1, 3)
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


def test_asyncio_driver_retransmits_and_expires(tmp_path: Path) -> None:
	"""Drive the generated coroutine on a real event loop and prove both
	behaviours: a retransmit that then correlates, and an expiry after the
	retry budget."""
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
