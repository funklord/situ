"""The `qt` driver: the rung-6 state machine inside Qt's event loop.

Decision 0033, which names Qt "the one this workspace will want first, three
private projects being Qt". A driver is an additive artifact -- `--driver qt`
adds `<name>_qt.hpp` and changes nothing else -- gated on the same test the
drive layer uses, so it emits nothing where no exchange states a policy.

THIS ONE DOES NOT OWN A LOOP, and that is the difference between an OS
facility and a host runtime. `epoll` and its siblings are handed a descriptor
and run until the exchange ends; Qt's event loop belongs to the application,
so this installs a `QSocketNotifier` and a single-shot `QTimer`, wires them to
the state machine, and returns. The caller's `QCoreApplication::exec()` pumps
it. The outcome therefore arrives at a completion callback rather than as a
return value -- the one place a driver changes the shape the *caller* sees,
which 0033 permits ("the driver absorbs the difference") and which is recorded
in its amendments so the next host runtime does not reinvent it.

NOTHING EMITTED HERE DECLARES Q_OBJECT, A SIGNAL OR A SLOT. That is a
requirement rather than a style: a consumer must not have to run `moc` over
generated code, which would put a build step inside situ's output. Every
connection is made to a lambda with a plain `QObject` member as context, so
only Qt's own already-moc'd classes need a metaobject, and destroying the
pump drops both connections though the pump is not a `QObject` itself.

The clock is the driver's, never the state machine's, and it is monotonic:
`step`'s deadlines are `now + timeout`, so a wall clock stepped by NTP or a
timezone change would fire every retransmission at once or suspend them for
hours.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.drive import driven
from situc.codegen.c.names import c_name
from situc.codegen.cpp.names import class_name
from situc.resolve import ResolvedSchema
from situc import __version__

__all__ = ["generate"]


def _reply_view(relation: ast.Relation,
		resolved: ResolvedSchema) -> tuple[str, bool]:
	"""The reply message's C++ class, and whether its `at` is the fixed form.

	`on_message` reads the conversation key off a view of the reply, so the
	pump acquires one over the received datagram first. A fixed struct knows
	its own extent and takes `at(owner, offset, out)`; a frame takes the
	received length too (`cpp/emit.py:_view_acquisition`).
	"""
	reply = relation.params[1]
	struct = resolved.structs[reply.type_name]
	return class_name(struct), struct.layout.is_fixed_size


def _shared() -> list[str]:
	"""The submit side, the clock and the callback types -- one copy, since
	none of them depends on which relation is being driven."""
	return [
		"/** The submit side: a connected datagram fd. Hand one to the drive",
		" * machine's constructor in place of the transcript a test uses. */",
		"class qt_io final : public ::situ::io {",
		"public:",
		"\texplicit qt_io(int fd) noexcept : fd_(fd) {}",
		"",
		"\t/* A datagram send is all-or-nothing, so a full send buffer",
		"\t * (EAGAIN) is a dropped datagram the retry budget recovers, and an",
		"\t * interrupt (EINTR) is retried; only a hard error propagates.",
		"\t * MSG_NOSIGNAL because a dead peer must not SIGPIPE an",
		"\t * application whose event loop this is. */",
		"\t[[nodiscard]] ::situ::rt::err submit(const std::uint8_t *data,",
		"\t                                     std::uint32_t len) noexcept"
		" override",
		"\t{",
		"\t\tfor (;;) {",
		"\t\t\tconst ssize_t sent = ::send(fd_, data, len, MSG_NOSIGNAL);",
		"\t\t\tif (sent >= 0) {",
		"\t\t\t\treturn ::situ::rt::err::ok;",
		"\t\t\t}",
		"\t\t\tif (errno == EINTR) {",
		"\t\t\t\tcontinue;",
		"\t\t\t}",
		"\t\t\tif (errno == EAGAIN || errno == EWOULDBLOCK) {",
		"\t\t\t\treturn ::situ::rt::err::ok;",
		"\t\t\t}",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		"\t}",
		"",
		"\t[[nodiscard]] int fd() const noexcept { return fd_; }",
		"",
		"private:",
		"\tint fd_;",
		"};",
		"",
		"/** The clock, read only by the driver and never by the state",
		" * machine. Monotonic, and it wraps at 2^32 ms -- which is fine,",
		" * every deadline comparison being a wrap-safe signed difference,",
		" * here and inside `step`. */",
		"[[nodiscard]] inline std::uint32_t qt_now_ms() noexcept",
		"{",
		"\tusing namespace std::chrono;",
		"\tconst auto ms = duration_cast<milliseconds>(",
		"\t\tsteady_clock::now().time_since_epoch()).count();",
		"\treturn static_cast<std::uint32_t>(static_cast<std::uint64_t>(ms));",
		"}",
		"",
		"/** A correlated reply, a batch of exchanges that ran out of retries,",
		" * and completion. Any of them may be empty. */",
		"using qt_reply_fn   = std::function<void(std::uint32_t id)>;",
		"using qt_expired_fn = std::function<void(std::uint32_t count)>;",
		"using qt_done_fn    = std::function<void(::situ::rt::err rc)>;",
		"",
	]


def _pump(relation: ast.Relation, resolved: ResolvedSchema) -> list[str]:
	"""The pump and its convenience runner, for one driven relation."""
	drive = f"{c_name(relation.name)}_driver"
	pump  = f"qt_{c_name(relation.name)}_pump"
	run   = f"qt_{c_name(relation.name)}_run"
	view, fixed = _reply_view(relation, resolved)

	acquire = (f"{view}::at(owner, 0u, reply)" if fixed
	           else f"{view}::at(owner, 0u, "
	                "static_cast<std::uint32_t>(got), reply)")

	return [
		f"/** Drives `{relation.name}` inside the caller's event loop.",
		" *",
		" * Owns a QSocketNotifier and a single-shot QTimer as value members;",
		" * both connections take an inert QObject member as context, so they",
		" * drop when the pump dies though the pump is not a QObject itself.",
		" * Put one wherever the exchange's lifetime belongs -- a member of",
		f" * the window, a unique_ptr in the session object -- or use `{run}`",
		" * below, which heap-allocates one and deletes it after completion.",
		" *",
		" * Not reentrant against its own callbacks: `on_reply` and",
		" * `on_expired` run inside the handlers, so neither may destroy the",
		" * pump. Destroy it from `on_done`, or after it. */",
		f"class {pump} {{",
		"public:",
		f"\t{pump}(int fd, ::situ::{drive} &drive,",
		"\t                 qt_reply_fn on_reply, qt_expired_fn on_expired,",
		"\t                 qt_done_fn on_done = {})",
		"\t\t: fd_(fd), drive_(drive),",
		"\t\t  on_reply_(std::move(on_reply)),",
		"\t\t  on_expired_(std::move(on_expired)),",
		"\t\t  on_done_(std::move(on_done)), finished_(false),",
		"\t\t  context_(), notifier_(fd, QSocketNotifier::Read), timer_()",
		"\t{",
		"\t\t/* A QSocketNotifier is live from construction, and nothing is",
		"\t\t * outstanding until `start`. */",
		"\t\tnotifier_.setEnabled(false);",
		"",
		"\t\ttimer_.setSingleShot(true);",
		"\t\t/* Precise, not Qt's default coarse timer: a coarse one may move",
		"\t\t * an interval by up to 5% to coalesce wakeups, and this interval",
		"\t\t * is a retransmission deadline the schema stated. */",
		"\t\ttimer_.setTimerType(Qt::PreciseTimer);",
		"",
		"\t\t/* The two connections, and the whole reason no moc runs over",
		"\t\t * this file: the signals belong to Qt's own already-moc'd",
		"\t\t * classes and the receivers are lambdas rather than slots of",
		"\t\t * ours. `context_` ties the connections' lifetime to this",
		"\t\t * object -- a plain QObject, never a sender. */",
		"\t\tQObject::connect(&notifier_, &QSocketNotifier::activated,",
		"\t\t                 &context_, [this]() { this->on_readable(); });",
		"\t\tQObject::connect(&timer_, &QTimer::timeout, &context_,",
		"\t\t                 [this]() { this->on_deadline(); });",
		"\t}",
		"",
		f"\t{pump}(const {pump} &) = delete;",
		f"\t{pump} &operator=(const {pump} &) = delete;",
		"",
		"\tvoid set_on_done(qt_done_fn on_done)"
		" { on_done_ = std::move(on_done); }",
		"",
		"\t/** Arm the notifier and queue the first `step`, then return.",
		"\t * Completion is always asynchronous: `on_done` never fires on this",
		"\t * stack, not even when nothing is outstanding -- a caller must not",
		"\t * have to defend against being destroyed inside its own setup. */",
		"\t[[nodiscard]] ::situ::rt::err start() noexcept",
		"\t{",
		"\t\t/* The fd must not block: this runs on the thread that draws, so",
		"\t\t * a recv on a spurious readiness would stop the application.",
		"\t\t * Non-blocking turns that into EAGAIN, which the retry budget",
		"\t\t * already covers. */",
		"\t\tconst int flags = ::fcntl(fd_, F_GETFL, 0);",
		"\t\tif (flags < 0 || ::fcntl(fd_, F_SETFL, flags | O_NONBLOCK) < 0) {",
		"\t\t\treturn ::situ::rt::err::bounds;",
		"\t\t}",
		"",
		"\t\tfinished_ = false;",
		"\t\tnotifier_.setEnabled(true);",
		"\t\tQMetaObject::invokeMethod(&context_, [this]() { (void)rearm(); },",
		"\t\t                          Qt::QueuedConnection);",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"",
		"\t/** Disarm without completing. Idempotent. */",
		"\tvoid stop() noexcept",
		"\t{",
		"\t\tnotifier_.setEnabled(false);",
		"\t\ttimer_.stop();",
		"\t}",
		"",
		"\t/** Delete this pump once the current emission has unwound: the",
		"\t * queued call is delivered to the pump's own context object, which",
		"\t * is QObject::deleteLater's pattern for a type that is not one. */",
		"\tvoid delete_later()",
		"\t{",
		"\t\tQMetaObject::invokeMethod(&context_, [this]() { delete this; },",
		"\t\t                          Qt::QueuedConnection);",
		"\t}",
		"",
		"private:",
		"\tvoid on_readable()",
		"\t{",
		"\t\t/* A QSocketNotifier is level-triggered and fires for as long as",
		"\t\t * the descriptor stays readable, so a handler that re-enters the",
		"\t\t * event loop -- a dialog, a nested QEventLoop in a callback --",
		"\t\t * would be re-entered with it. Disable for the duration. */",
		"\t\tnotifier_.setEnabled(false);",
		"",
		"\t\tstd::uint8_t buf[2048];",
		"\t\tconst ssize_t got = ::recv(fd_, buf, sizeof buf, 0);",
		"\t\tif (got < 0) {",
		"\t\t\tif (errno == EINTR || errno == EAGAIN",
		"\t\t\t                || errno == EWOULDBLOCK) {",
		"\t\t\t\tnotifier_.setEnabled(true);\t/* readiness was spurious */",
		"\t\t\t\treturn;",
		"\t\t\t}",
		"\t\t\tfinish(::situ::rt::err::bounds);",
		"\t\t\treturn;",
		"\t\t}",
		"\t\tif (got == 0) {",
		"\t\t\tnotifier_.setEnabled(true);",
		"\t\t\treturn;\t/* an empty datagram is not a message */",
		"\t\t}",
		"",
		"\t\t/* One datagram is one message: acquire a view over it at offset",
		"\t\t * zero and hand that straight to the state machine. A frame too",
		"\t\t * short to be the reply is dropped, as an uncorrelated one is. */",
		"\t\t::situ::rt::message owner(buf,"
		" static_cast<std::uint32_t>(got));",
		f"\t\t::situ::{view} reply;",
		f"\t\tif (::situ::{acquire} == ::situ::rt::err::ok) {{",
		"\t\t\tstd::uint32_t id = 0u;",
		"\t\t\tif (drive_.on_message(reply, id) == ::situ::rt::err::ok",
		"\t\t\t                && on_reply_) {",
		"\t\t\t\ton_reply_(id);",
		"\t\t\t}",
		"\t\t}",
		"",
		"\t\tnotifier_.setEnabled(true);",
		"\t\t(void)rearm();",
		"\t}",
		"",
		"\tvoid on_deadline() { (void)rearm(); }",
		"",
		"\t[[nodiscard]] ::situ::rt::err rearm() noexcept",
		"\t{",
		"\t\tif (finished_) {",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		"",
		"\t\tconst std::uint32_t now = qt_now_ms();",
		"\t\tstd::uint32_t next_ms = 0u;",
		"\t\tstd::uint32_t expired = 0u;",
		"",
		"\t\t/* Retransmit what is due, expire what is spent, and learn the",
		"\t\t * earliest remaining deadline. `truncated` is an empty in-flight",
		"\t\t * set: no deadline to wait on and the exchange is done. */",
		"\t\tconst ::situ::rt::err stepped = drive_.step(now, next_ms,"
		" expired);",
		"\t\tif (expired != 0u && on_expired_) {",
		"\t\t\ton_expired_(expired);",
		"\t\t}",
		"\t\tif (stepped == ::situ::rt::err::truncated) {",
		"\t\t\tfinish(::situ::rt::err::ok);",
		"\t\t\treturn ::situ::rt::err::ok;",
		"\t\t}",
		"\t\tif (stepped != ::situ::rt::err::ok) {",
		"\t\t\tfinish(stepped);",
		"\t\t\treturn stepped;",
		"\t\t}",
		"",
		"\t\t/* The interval is `next_ms - now`, floored at zero and",
		"\t\t * wrap-safe: a future deadline waits, a reached one fires on the",
		"\t\t * next turn of the loop and `step` retransmits then. */",
		"\t\tstd::int32_t diff = static_cast<std::int32_t>(next_ms - now);",
		"\t\tif (diff < 0) {",
		"\t\t\tdiff = 0;",
		"\t\t}",
		"\t\ttimer_.start(diff);",
		"\t\treturn ::situ::rt::err::ok;",
		"\t}",
		"",
		"\tvoid finish(::situ::rt::err rc)",
		"\t{",
		"\t\tif (finished_) {",
		"\t\t\treturn;",
		"\t\t}",
		"\t\tfinished_ = true;",
		"\t\tstop();",
		"\t\tif (on_done_) {",
		"\t\t\ton_done_(rc);",
		"\t\t}",
		"\t}",
		"",
		"\tint                      fd_;",
		f"\t::situ::{drive} &drive_;",
		"\tqt_reply_fn              on_reply_;",
		"\tqt_expired_fn            on_expired_;",
		"\tqt_done_fn               on_done_;",
		"\tbool                     finished_;",
		"",
		"\tQObject         context_;\t/* connection context; not a sender */",
		"\tQSocketNotifier notifier_;",
		"\tQTimer          timer_;",
		"};",
		"",
		f"/** Drive `{relation.name}` to completion inside the caller's event",
		" * loop. Installs the notifier and the timer and returns immediately",
		" * -- the running QCoreApplication::exec() pumps them.",
		" *",
		" * The return value reports only whether arming succeeded; the",
		" * exchange's outcome arrives at `on_done`, because a Qt driver",
		" * cannot block for it. The pump is deleted after `on_done`. */",
		f"[[nodiscard]] inline ::situ::rt::err {run}(",
		f"\t\tint fd, ::situ::{drive} &drive,",
		"\t\tqt_reply_fn on_reply, qt_expired_fn on_expired,",
		"\t\tqt_done_fn on_done = {})",
		"{",
		f"\tauto *pump = new {pump}(fd, drive, std::move(on_reply),",
		"\t                                  std::move(on_expired));",
		"",
		"\tpump->set_on_done([pump, done = std::move(on_done)]",
		"\t                  (::situ::rt::err rc) {",
		"\t\tif (done) {",
		"\t\t\tdone(rc);",
		"\t\t}",
		"\t\tpump->delete_later();",
		"\t});",
		"",
		"\tconst ::situ::rt::err armed = pump->start();",
		"\tif (armed != ::situ::rt::err::ok) {",
		"\t\t/* `start` fails only before it arms anything and never completes",
		"\t\t * on this stack, so nothing is queued: delete outright. */",
		"\t\tdelete pump;",
		"\t}",
		"\treturn armed;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> dict[str, str]:
	"""The Qt driver's header, or nothing where no exchange states a policy
	-- the same `driven()` gate the drive layer uses."""
	ready = driven(schema, resolved)
	if not ready:
		return {}

	guard = f"SITU_{c_name(basename).upper()}_QT_HPP"
	lines = [
		f"/* Generated by situc {__version__} from {basename}.situ -- do not"
		" edit.",
		" *",
		" * The Qt driver for rung 6 (decision 0033). Qt's event loop belongs",
		" * to the application, not to situ, so this installs a",
		" * QSocketNotifier and a single-shot QTimer, wires them to the state",
		" * machine, and returns -- the caller's exec() pumps it, and a",
		" * completion callback carries what the fd-based drivers return.",
		" *",
		" * NOTHING HERE DECLARES Q_OBJECT, A SIGNAL OR A SLOT: a consumer",
		" * must not have to run `moc` over generated code. Every connection",
		" * is made to a lambda with a plain QObject as context.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include <cstdint>",
		"#include <chrono>",
		"#include <functional>",
		"#include <utility>",
		"",
		"#include <cerrno>",
		"#include <fcntl.h>",
		"#include <sys/socket.h>",
		"#include <unistd.h>",
		"",
		"#include <QtCore/QMetaObject>",
		"#include <QtCore/QObject>",
		"#include <QtCore/QSocketNotifier>",
		"#include <QtCore/QTimer>",
		"",
		f"#include \"{basename}_drive.hpp\"",
		"",
		"namespace situ {",
		"",
		*_shared(),
	]
	for relation, _policy in ready:
		lines.extend(_pump(relation, resolved))
	lines += [
		"}  /* namespace situ */",
		"",
		f"#endif /* {guard} */",
	]
	return {f"{basename}_qt.hpp": "\n".join(lines) + "\n"}
