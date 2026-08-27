"""The `asyncio` driver: the rung-6 state machine on Python's own runtime.

Decision 0033, which names "`asyncio` in Python" among the host runtimes. A
driver is an additive artifact -- `--driver asyncio` adds `<name>_asyncio.py`
and changes nothing else -- gated on the same test the drive layer uses, so
it emits nothing where no exchange states a policy.

WHY THIS MODULE IS NOT CALLED `asyncio.py`. A generator named for the runtime
it targets would sit in `situc/codegen/python/` next to modules that import
the real one. Absolute imports mean it would not in fact shadow the standard
library, but a reader has to know that to be sure, and the generated module
carries the schema's name (`<name>_asyncio.py`) precisely so that it cannot
shadow either when a caller puts the output directory on their path.

THE HOST-RUNTIME SHAPE, AND WHERE IT DIVERGES FROM QT. Like the Qt driver,
this does not own the loop: the application's loop is already running, and a
driver that called `run_until_complete` would be taking it over. Unlike Qt,
the artifact is a *coroutine*, and a coroutine is the runtime's own way of
saying "this finishes later and yields a value" -- so the outcome is
returned, not delivered to a completion callback. Qt needed the callback
because a C++ function that installs a notifier has no way to return
something that has not happened yet; `await` is exactly that way. The
callbacks that remain (`on_reply`, `on_expired`) report events *during* the
exchange, which a single return value cannot carry in either backend.

The clock is the driver's, never the state machine's: `time.monotonic()`,
truncated to the same 32 bits every other backend uses, so a deadline
compares the same way here as in C. Monotonic because `step`'s deadlines are
`now + timeout`, and a wall clock stepped by NTP would fire every
retransmission at once or suspend them for hours.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.drive import driven
from situc.codegen.python.emit import py_name
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate"]


def _preamble(basename: str) -> list[str]:
	"""The clock and the submit helper -- one copy, since neither depends on
	which relation is driven."""
	return [
		"def _now_ms() -> int:",
		'\t"""The clock, read only by the driver and never by the state',
		"\tmachine. Monotonic, and truncated to 32 bits so that a deadline",
		"\tcompares here exactly as it does in the C backends -- which wrap",
		'\tat the same point, and whose comparisons are wrap-safe."""',
		"\treturn int(_time.monotonic() * 1000.0) & 0xFFFFFFFF",
		"",
		"",
		"def submit_to(sock: _socket.socket) -> Callable[[bytes], None]:",
		'\t"""The submit side: a connected datagram socket.',
		"",
		"\tA datagram send is all-or-nothing, so a full send buffer",
		"\t(BlockingIOError, which is EAGAIN) is a dropped datagram the retry",
		"\tbudget recovers, and an interrupt is retried. Only a hard error",
		'\tpropagates, and the state machine never sees the difference."""',
		"",
		"\tdef submit(data: bytes) -> None:",
		"\t\twhile True:",
		"\t\t\ttry:",
		"\t\t\t\tsock.send(data)",
		"\t\t\texcept InterruptedError:",
		"\t\t\t\tcontinue",
		"\t\t\texcept BlockingIOError:",
		"\t\t\t\tpass\t# let retransmission recover it",
		"\t\t\treturn",
		"",
		"\treturn submit",
		"",
	]


def _run(relation: ast.Relation) -> list[str]:
	"""The coroutine for one driven relation."""
	name  = py_name(relation.name)
	reply = py_name(relation.params[1].type_name)

	# Continuations align under the opening paren, which moves with the
	# relation's name: alignment is spaces after the indent, and the column
	# is the definition's own.
	pad = " " * len(f"async def run_{name}(")

	return [
		"",
		f"async def run_{name}(sock: _socket.socket, "
		f"drive: {name}_driver,",
		f"{pad}on_reply: Callable[[int], None] | None = None,",
		f"{pad}on_expired: Callable[[int], None] | None = None,",
		f"{pad}) -> None:",
		f'\t"""Drive `{relation.name}` to completion on the running loop.',
		"",
		"\tReturns when nothing is outstanding, which is `step` answering a",
		"\tdeadline of None: completion, not an error. The outcome is the",
		"\treturn rather than a callback because a coroutine can carry one --",
		"\tthe Qt driver cannot, and says so (0033).",
		"",
		"\t`on_reply` fires with the caller's handle when a reply correlates,",
		"\t`on_expired` with a count when exchanges run out of retries. Those",
		"\tare events during the exchange, which no single return value could",
		'\tcarry in any backend."""',
		"\tloop = _asyncio.get_running_loop()",
		"\tsock.setblocking(False)",
		"",
		"\t# One waiter, re-armed each turn. `add_reader` rather than",
		"\t# `sock_recv` because the deadline has to race the datagram: a",
		"\t# reader that is merely awaited cannot be given a timeout without",
		"\t# cancelling it, and a cancelled `sock_recv` can consume a datagram",
		"\t# it then drops. A future the reader resolves is cancellable with",
		"\t# nothing in flight to lose.",
		"\treadable: _asyncio.Future[None] | None = None",
		"",
		"\tdef _wake() -> None:",
		"\t\tif readable is not None and not readable.done():",
		"\t\t\treadable.set_result(None)",
		"",
		"\tloop.add_reader(sock.fileno(), _wake)",
		"\ttry:",
		"\t\twhile True:",
		"\t\t\t# Retransmit what is due, expire what is spent, and learn the",
		"\t\t\t# earliest remaining deadline. A deadline of None is an empty",
		"\t\t\t# in-flight set: nothing to wait for and the exchange is done.",
		"\t\t\tnext_ms, expired = drive.step(_now_ms())",
		"\t\t\tif expired and on_expired is not None:",
		"\t\t\t\ton_expired(expired)",
		"\t\t\tif next_ms is None:",
		"\t\t\t\treturn",
		"",
		"\t\t\t# The wait is `next_ms - now`, floored at zero and wrap-safe:",
		"\t\t\t# a future deadline waits, a reached one polls and `step`",
		"\t\t\t# retransmits on the next turn.",
		"\t\t\tdiff = (next_ms - _now_ms()) & 0xFFFFFFFF",
		"\t\t\tif diff > 0x7FFFFFFF:\t# the deadline is behind us",
		"\t\t\t\tdiff = 0",
		"",
		"\t\t\treadable = loop.create_future()",
		"\t\t\ttry:",
		"\t\t\t\tawait _asyncio.wait_for(readable, diff / 1000.0)",
		"\t\t\texcept (TimeoutError, _asyncio.TimeoutError):",
		"\t\t\t\tcontinue\t# `step` retransmits on the next turn",
		"\t\t\tfinally:",
		"\t\t\t\treadable = None",
		"",
		"\t\t\ttry:",
		"\t\t\t\tdata = sock.recv(65535)",
		"\t\t\texcept (BlockingIOError, InterruptedError):",
		"\t\t\t\tcontinue\t# the readiness was spurious",
		"\t\t\tif not data:",
		"\t\t\t\tcontinue\t# an empty datagram is not a message",
		"",
		"\t\t\t# One datagram is one message: acquire a view over it at offset",
		"\t\t\t# zero and hand that straight to the state machine. A frame too",
		"\t\t\t# short to be the reply is dropped, as an uncorrelated one is.",
		"\t\t\ttry:",
		f"\t\t\t\treply = {reply}.at(Message(bytearray(data)), 0)",
		"\t\t\texcept BoundsError:",
		"\t\t\t\tcontinue",
		"\t\t\ttry:",
		"\t\t\t\tanswered = drive.on_message(reply)",
		"\t\t\texcept ConstraintError:",
		"\t\t\t\tcontinue\t# nothing outstanding matches it",
		"\t\t\tif on_reply is not None:",
		"\t\t\t\ton_reply(answered)",
		"\tfinally:",
		"\t\tloop.remove_reader(sock.fileno())",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""The asyncio driver's module, or nothing where no exchange states a
	policy -- the same `driven()` gate the drive layer uses."""
	ready = driven(schema, resolved)
	if not ready:
		return {}

	lines = [
		f'"""Generated by situc {__version__} from {basename}.situ'
		' -- do not edit.',
		"",
		"The asyncio driver for rung 6 (decision 0033). The running loop is",
		"the application's, not situ's, so nothing here starts or stops one:",
		"each coroutine registers a reader, races the state machine's deadline",
		"against the next datagram, and returns when the exchange is over.",
		"",
		"The clock is the driver's and never the state machine's, which is",
		"what lets the same ten simulated minutes answer the same in every",
		"backend.",
		'"""',
		"",
		"from __future__ import annotations",
		"",
		"import asyncio as _asyncio",
		"import socket as _socket",
		"import time as _time",
		"from typing import Callable",
		"",
		"from situ_runtime import BoundsError, ConstraintError, Message",
		"",
		f"from {basename}_drive import *  # noqa: F403",
		"",
		"",
		*_preamble(basename),
	]
	for relation, _policy in ready:
		lines.extend(_run(relation))
	return {f"{basename}_asyncio.py": "\n".join(lines).rstrip("\n") + "\n"}
