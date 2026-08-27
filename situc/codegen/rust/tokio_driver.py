"""The `tokio` driver: the rung-6 state machine inside a tokio runtime.

Decision 0033, which names "`tokio`, `async-std` or `embassy` in Rust" among
the host runtimes. A driver is an additive artifact -- `--driver tokio` adds
`<name>_tokio.rs` and changes nothing else -- gated on the same test the drive
layer uses, so it emits nothing where no relation states a policy.

A HOST RUNTIME, AND THE SECOND SHAPE IT CAN TAKE. The Qt driver could not
return the exchange's outcome: Qt's loop belongs to the application, so the
driver installs a notifier and a timer and returns immediately, leaving the
outcome to a completion callback. An async runtime is the other resolution of
the same problem. An `async fn` *is* the awaitable -- the caller drives it
with `.await`, inside whatever runtime it already has -- so the outcome comes
back as a return value after all, and only the per-event reports (a reply
correlated, exchanges expired) need callbacks. Both are 0033's "the driver
absorbs the difference"; they differ because the runtimes do.

WHY THE SEND IS NOT `try_send`. `Io::submit` is synchronous -- the state
machine calls it from inside `send` and `step` -- so a driver cannot await in
it, and tokio's `UdpSocket::try_send` looks like the non-blocking send that
fits. It is not. `try_send` answers `WouldBlock` out of tokio's own readiness
bookkeeping *without attempting the syscall*, and that readiness is re-armed
only by the runtime, which cannot run while this callback holds the thread.
`step` has already spent a retry by the time it calls `submit`, so a
`WouldBlock` swallowed here costs the datagram and its retransmission
together. Measured, before the fix: an exchange that should have sent three
datagrams sent two and then expired, about one run in six.

So the sink sends through a *std* socket sharing the same descriptor. That
performs the real `send(2)`, which means EAGAIN means what it means in every
other driver -- the kernel send buffer is genuinely full -- and that is the
case the retry budget is there to cover. `AsyncFd` would force an await into
`submit` and buy nothing.

WHY THE DESCRIPTOR IS SHARED. The generated driver *owns* its `Io` by value
(`ReplyToDriver { io: I }`), and the run function needs the same socket to
receive on. A `try_clone` of the socket gives the sink its own handle to the
same descriptor -- no lock, no second socket, and no split that would put the
two halves out of step.

The clock is the driver's, never the state machine's, and it is monotonic:
`step`'s deadlines are `now + timeout`, so a wall clock stepped by NTP would
fire every retransmission at once or suspend them for hours.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.rust.drive import _policy
from situc.codegen.rust.emit import _pascal
from situc.relation import Refused, key_layout
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate"]


def _driven(schema: ast.Schema,
		resolved: ResolvedSchema) -> list[tuple[ast.Relation, tuple[int, int]]]:
	"""Every relation that states a policy and can carry a table -- the same
	gate `rust/drive.py` applies, so the driver covers exactly the exchanges
	the drive layer emitted a machine for and no others."""
	ready = []
	for relation in schema.relations():
		policy = _policy(relation)
		if policy is None:
			continue
		try:
			key_layout(relation, resolved)
		except Refused:
			continue
		ready.append((relation, policy))
	return ready


def _shared(basename: str) -> list[str]:
	"""The sink and the clock -- one copy, since neither depends on which
	relation is being driven."""
	return [
		"/// Where the bytes go: a datagram socket shared with the loop that",
		"/// receives on it. Hand one to the drive machine's `new` in place of",
		"/// the transcript a test substitutes.",
		"///",
		"/// The submit side.",
		"///",
		"/// It sends through a *std* socket sharing the tokio socket's",
		"/// descriptor, not through `tokio::net::UdpSocket::try_send`, and the",
		"/// difference is not stylistic. `try_send` answers `WouldBlock` from",
		"/// tokio's own readiness bookkeeping without attempting the syscall,",
		"/// and that readiness is only re-armed by the runtime -- which cannot",
		"/// run inside this synchronous callback. `step` has already spent a",
		"/// retry by the time it calls `submit`, so a `WouldBlock` reported",
		"/// here costs the datagram *and* its retransmission: measured, an",
		"/// exchange that should have sent three datagrams sent two and then",
		"/// expired. The std socket performs the real `send(2)`, so EAGAIN",
		"/// means what it means in every other driver -- the kernel buffer is",
		"/// genuinely full -- and that is the case the retry budget covers.",
		"pub struct TokioIo {",
		"\tsend_on: std::sync::Arc<std::net::UdpSocket>,",
		"}",
		"",
		"impl TokioIo {",
		"\t/// `send_on` must be connected to the peer and share nothing with",
		"\t/// tokio's readiness -- a `try_clone` of the socket the runtime",
		"\t/// receives on is exactly right, the descriptor being the same one.",
		"\tpub fn new(send_on: std::sync::Arc<std::net::UdpSocket>) -> Self {",
		"\t\tSelf { send_on }",
		"\t}",
		"}",
		"",
		f"impl {basename}_drive::Io for TokioIo {{",
		"\tfn submit(&mut self, data: &[u8]) -> situ_rt::Result<()> {",
		"\t\tmatch self.send_on.send(data) {",
		"\t\t\tOk(_) => Ok(()),",
		"\t\t\t// The buffer is full: let retransmission recover it.",
		"\t\t\tErr(e) if e.kind() == std::io::ErrorKind::WouldBlock => Ok(()),",
		"\t\t\tErr(e) if e.kind() == std::io::ErrorKind::Interrupted => Ok(()),",
		"\t\t\tErr(_) => Err(situ_rt::Error::Bounds),",
		"\t\t}",
		"\t}",
		"}",
		"",
		"/// The clock, read only by the driver and never by the state machine.",
		"/// Monotonic, and it wraps at 2^32 ms -- which is fine, every deadline",
		"/// comparison being a wrap-safe signed difference here and inside",
		"/// `step`.",
		"pub fn now_ms(base: std::time::Instant) -> u32 {",
		"\tbase.elapsed().as_millis() as u32",
		"}",
		"",
	]


def _run(relation: ast.Relation, basename: str) -> list[str]:
	"""The async loop for one driven relation."""
	held = _pascal(relation.name)
	run  = f"run_{relation.name}"
	_, second = relation.params
	resp = _pascal(second.type_name)

	return [
		f"/// Drive `{relation.name}` to completion inside the caller's tokio",
		"/// runtime.",
		"///",
		"/// Unlike a driver for a loop it does not own, this returns the",
		"/// outcome: an `async fn` is the awaitable, so the caller `.await`s it",
		"/// and gets `Ok(())` when nothing is outstanding -- `step` answering",
		"/// `Error::Truncated`, which is completion rather than an error.",
		"/// `on_reply` fires with the caller's handle when a reply correlates,",
		"/// `on_expired` with a count when exchanges run out of retries.",
		"///",
		"/// The socket is the one the sink sends on: pass the same `Arc` given",
		"/// to `TokioIo::new`, so what is received is what was sent to.",
		f"pub async fn {run}<I, R, E>(",
		"\tsocket: &tokio::net::UdpSocket,",
		f"\tdrive: &mut {basename}_drive::{held}Driver<'_, '_, I>,",
		"\tbase: std::time::Instant,",
		"\tmut on_reply: R,",
		"\tmut on_expired: E,",
		") -> situ_rt::Result<()>",
		"where",
		f"\tI: {basename}_drive::Io,",
		"\tR: FnMut(u32),",
		"\tE: FnMut(u32),",
		"{",
		"\tlet mut buf = [0u8; 2048];",
		"\tloop {",
		"\t\t// Retransmit what is due, expire what is spent, and learn the",
		"\t\t// earliest remaining deadline. A `None` deadline is an empty",
		"\t\t// in-flight set: nothing to wait on, so the exchange is done.",
		"\t\t//",
		"\t\t// The clock is read once and both `step` and the wait are",
		"\t\t// computed from that same instant. Reading it twice let the",
		"\t\t// deadline `step` had just set be measured against a later now,",
		"\t\t// which shortens every wait by however long the call took.",
		"\t\tlet now = now_ms(base);",
		"\t\tlet (next, expired) = drive.step(now);",
		"\t\tif expired != 0 {",
		"\t\t\ton_expired(expired);",
		"\t\t}",
		"\t\tlet next_ms = match next {",
		"\t\t\tSome(at) => at,",
		"\t\t\tNone => return Ok(()),",
		"\t\t};",
		"",
		"\t\t// The wait is `next_ms - now`, floored at zero and wrap-safe: a",
		"\t\t// future deadline waits, a reached one polls and `step`",
		"\t\t// retransmits on the next turn.",
		"\t\tlet diff = (next_ms.wrapping_sub(now)) as i32;",
		"\t\tlet wait = if diff < 0 { 0u64 } else { diff as u64 };",
		"",
		"\t\t// A receive raced against the deadline. Whichever finishes first",
		"\t\t// wins and the loop re-`step`s, which is where a timeout becomes",
		"\t\t// a retransmission.",
		"\t\ttokio::select! {",
		"\t\t\tgot = socket.recv(&mut buf) => {",
		"\t\t\t\tlet n = match got {",
		"\t\t\t\t\tOk(n) => n,",
		"\t\t\t\t\t// A datagram nobody can read is not a message; the",
		"\t\t\t\t\t// deadline still governs.",
		"\t\t\t\t\tErr(_) => continue,",
		"\t\t\t\t};",
		"\t\t\t\t// One datagram is one message: a view over it, handed",
		"\t\t\t\t// straight to the state machine. A frame too short to be",
		"\t\t\t\t// the reply is dropped, as an uncorrelated one is.",
		f"\t\t\t\tif let Ok(reply) = {basename}::{resp}::new(&buf[..n]) {{",
		"\t\t\t\t\tif let Ok(id) = drive.on_message(&reply) {",
		"\t\t\t\t\t\ton_reply(id);",
		"\t\t\t\t\t}",
		"\t\t\t\t}",
		"\t\t\t}",
		"\t\t\t_ = tokio::time::sleep(",
		"\t\t\t\t\tstd::time::Duration::from_millis(wait)) => {}",
		"\t\t}",
		"\t}",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""The tokio driver's module, or nothing where no exchange states a
	policy -- the same gate the drive layer uses."""
	ready = _driven(schema, resolved)
	if not ready:
		return {}

	lines = [
		f"// Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		"//",
		"// The tokio driver for rung 6 (decision 0033). A host runtime rather",
		"// than an OS facility: the caller already has a runtime, so each",
		"// exchange is an `async fn` it awaits. The driver owns the socket and",
		"// the clock; the state machine owns retransmission, correlation and",
		"// expiry and reaches I/O only through the `Io` trait.",
		"",
		f"use crate::{basename};",
		f"use crate::{basename}_drive;",
		"use crate::situ_rt;",
		"",
		*_shared(basename),
	]
	for relation, _policy_of in ready:
		lines.extend(_run(relation, basename))
	return {f"{basename}_tokio.rs": "\n".join(lines).rstrip("\n") + "\n"}
