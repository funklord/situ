# 0033: drivers are a third axis, and the vtable is completion-shaped

Status: accepted, and four drivers are built -- `--driver epoll`, `poll` and
`blocking` (readiness and no-multiplexer) and `--driver io_uring` (completion,
the one the vtable was shaped for), all C-only over `--layer drive` (see the
amendments below). The taxonomy and the two shape calls were decided here;
the copyright holder then asked for these built.
Date: 2026-08-08
Phase: 26.98 gains the deadline return; epoll/poll/blocking/io_uring built
2026-08-26

## Context

Rung 6 of the layer ladder (0032) sends, receives, retransmits and times out.
It is already **sans-I/O**: 26.98 gives it a caller-supplied vtable and a step
function taking `now_ms`, and forbids it from reading the clock. That was
argued for on testability grounds, and it has a second consequence nobody
claimed at the time -- the generated state machine **cannot tell `epoll` from
`select` from a blocking socket from a Qt event loop**. The multiplexing
choice was already the caller's.

So the question this record answers is not which scheduler situ uses. It is
whether situ *ships the adapters*, and by the test that started this whole
direction -- remove code from other network projects generically and
efficiently -- it should. An `epoll` loop is exactly the eighty lines three
projects each hand-write, and exactly the kind where one of them gets the
edge-triggered case subtly wrong.

## Decision

**A driver is an additive generated artifact, selected by `--driver`, and the
state machine is unaware of them.** This is a third axis, beside the two 0032
already names:

| axis | flag | what it chooses |
|---|---|---|
| layer | `--layer` | which invariants the output holds to |
| shape | `--owned`, `--materialize`, `--single-file` | what form the same information takes |
| driver | `--driver` | what pumps the rung-6 state machine |

The relationship is the one `gen-dissector` already has to the accessors: an
optional artifact over the same resolved schema. `--driver epoll` adds
`<name>_epoll.c` and changes nothing else, so 0032's additivity invariant
holds across the driver axis too, and the list may grow forever without the
core learning a single new fact.

**The test harness is just another driver.** A transcript driver injects loss,
reorder and duplication with no socket and no clock, so the tested path and
the shipped path differ *only* in which driver they link. That is what
`suggestion/fuzznet.md` asked for when it separated owning I/O from owning
the clock, arriving as a consequence of the axis rather than as a special
case.

## The space, which is larger than the obvious four

Named in full because the point of an axis is that it does not have to be
revisited per addition, and because an enumeration written once is what stops
the second driver being designed as though it were the last.

**Readiness-based** -- the facility says a descriptor is ready and the caller
does the syscall:

| | where | notes |
|---|---|---|
| `select` | POSIX, everywhere | `FD_SETSIZE` is a cliff, not a slope; the set is rebuilt per call |
| `poll` | POSIX | no descriptor ceiling; O(n) scan |
| `ppoll` | Linux, BSD | `poll` plus a signal mask and nanosecond resolution |
| `epoll` | Linux 2.6+ | persistent registration, level or edge triggered |
| `kqueue` | FreeBSD, OpenBSD, NetBSD, macOS | carries timers, signals and vnodes natively |
| event ports | Solaris, illumos | `port_getn` |
| `pollset` | AIX | |

**Completion-based** -- the caller submits an operation and is told when it
finished:

| | where | notes |
|---|---|---|
| `io_uring` | Linux 5.1+ | submission and completion rings, batched syscalls, registered buffers |
| IOCP | Windows | the native model there |
| RIO | Windows | registered I/O, for the latency tail |
| POSIX AIO | POSIX | widely implemented, rarely well |

**No multiplexing at all:**

| | notes |
|---|---|
| blocking | one socket, `read` and `write`; needs no OS facility beyond those |
| busy poll | non-blocking plus a spin, for latency floors and bare metal |
| interrupt-driven | an ISR feeds the state machine; the embedded shape |

**Kernel bypass and embedded stacks:** DPDK, netmap, AF_XDP, lwIP, and the
Zephyr and FreeRTOS socket APIs. These are drivers in exactly the same sense
and are named so that the vtable is not designed as though POSIX were the
world.

**Host runtimes, which are not OS facilities and are where the code actually
has to live.** situ has four backends, and a rung-6 driver for three of them
means a runtime rather than a syscall: `asyncio` in Python, `tokio`,
`async-std` or `embassy` in Rust, and Qt's `QSocketNotifier` with `QTimer` in
C++ -- the last being the one this workspace will want first, three private
projects being Qt.

**So the driver axis crosses the backend axis**, and not every cell is
meaningful. `--driver epoll --target python` is a worse asyncio; `--driver
io_uring --target python` is nothing at all. A driver therefore declares which
backends it is available for, and asking for an unavailable pair is refused
naming both -- the same shape as every other refusal here.

## Two shape calls, decided now because they are invasive later

**The step function returns the next deadline.** `epoll_wait`, `select`,
`poll` and every other facility above takes a timeout, and the state machine
is the only thing that knows when it next needs waking. Without this, every
driver invents a polling interval and the timing contract quietly stops being
the schema's -- which would undo the reason the contract is in the schema at
all (0032). This is the one thing the core must provide for any driver to
work, so 26.98 gains it.

Facilities that carry timers natively -- `kqueue`'s `EVFILT_TIMER`, Linux
`timerfd`, `io_uring` timeouts -- map the deadline onto those; the ones that
do not pass it as the wait timeout. The driver absorbs the difference, which
is what a driver is for.

**The vtable is completion-shaped: submit, then complete.** Readiness is the
more familiar model and the wrong one to build on. A readiness loop implements
completion trivially -- do the syscall when the descriptor is ready -- while a
completion loop cannot be retrofitted into a readiness API, because there is
no moment at which `io_uring` or IOCP will tell you a descriptor is *ready*.
Choosing readiness would exclude the entire second table above, permanently
and invisibly, and that exclusion is not one to discover at the fifth driver.

The cost is real: submit-then-complete puts ceremony into the simple cases,
and a blocking driver has to pretend an operation completed asynchronously
when it did not. That ceremony lives in the driver, which is written once
here, rather than in the consumer.

## The hard part, named rather than solved

**Completion means something other than the caller owns a buffer while the
operation is in flight**, and situ's whole model is that a view is a base, a
limit and a generation counter over memory the caller owns. A kernel writing
into that buffer between submit and complete is a mutation no setter was
called for, so the staleness machinery (12.3) has nothing to invalidate on and
a view could be read mid-write while reporting itself live.

`io_uring`'s registered buffers make this sharper rather than softer. This
also reaches rung 2: whatever supplies the backing at `--layer edit` is what
the kernel would be handed.

No answer here. Candidates are a buffer state the view model understands, a
generation bump at submit, or a rule that an in-flight buffer cannot be
viewed at all -- and picking between them wants a real driver in hand. It is
recorded because a completion-shaped vtable makes this reachable, and shipping
one without having asked the question would be the expensive order.

## Which to build, and how little is known

Deliberately not decided here, but the reasoning available today:

- **`blocking`** is the proof the state machine is drivable at all, needing no
  OS facility beyond `read` and `write`.
- **`poll`** is the smallest real multiplexer with no descriptor ceiling, and
  is POSIX rather than Linux.
- **`io_uring`** is the one whose *model* the vtable had to be shaped for, so
  building it early is what proves the shape rather than assuming it.
- **`select`** is strictly worse than `poll` wherever `poll` exists and its
  descriptor limit is a cliff. It earns its place only from a consumer who
  actually needs it.

**No consumer evidence exists yet, and that is the honest state.** The
suggestion files were searched for what the three candidate consumers actually
use: one mention of "event loop" in `fuzzypickles.md`, about view lifetimes
rather than multiplexing, and nothing else. So the ordering above reasons from
the facilities and not from a counted need. Ask the consumers before building
past the first two.

## Alternatives considered

**No drivers -- ship the vtable and let consumers write their loops.** Zero
variants to maintain, and it fails the test that started this: an `epoll` loop
is precisely the generic, efficient thing worth removing from three projects.

**A readiness-shaped vtable.** Simpler for the four familiar facilities and
permanently excludes the completion ones. Rejected above.

**Drivers as rungs on the layer ladder.** They grant no new permission -- rung
6 already owns I/O -- so they are not an invariant statement and do not belong
on that axis.

**A driver named in the schema.** Fails 0032's test directly: two endpoints
running one protocol may use `io_uring` and Qt respectively and interoperate
perfectly, so it is not something both ends must agree on, so it is not wire
contract.

## Consequences

- 26.98 gains the deadline return, and it is a core requirement rather than a
  driver's.
- The vtable is completion-shaped from the first driver, including the
  blocking one.
- A driver declares the backends it is available for; an unavailable pair is
  refused naming both.
- The transcript driver is how rung 6 is tested, so it is not optional
  scaffolding but the first driver written.
- The interaction between in-flight buffers and the view model is open, and
  reaches rung 2.

## Amendment, 2026-08-26: epoll, the first driver built

`--driver epoll` is built, C-only, over `--layer drive`. It is the additive
artifact this record named -- `gen-dissector`'s relationship to the accessors
-- adding `<name>_epoll.h` and `<name>_epoll.c` and changing nothing else,
gated on the same `driven()` test the drive layer uses, so it emits nothing
where no exchange states a policy. The generator is `situc/codegen/c/epoll.py`.

The loop is the division of labour 26.98 was built for: it owns the socket,
the clock (`CLOCK_MONOTONIC`) and the timer arithmetic; the state machine owns
retransmission, correlation and expiry and reaches I/O only through the
`situ_io_t` submit vtable. `step`'s next deadline is the `epoll_wait` timeout,
a wrap-safe signed difference so a wrapping monotonic clock is harmless, and
`step` answering `SITU_ERR_TRUNCATED` -- nothing in flight -- is the loop's
clean exit. It is level-triggered (one recv per wakeup) and datagram-oriented
(one recv is one message; a stream is rung 4's `_frame.h`, a different
driver's concern), and its submit swallows `EAGAIN` as a dropped datagram the
retry budget recovers.

The in-flight-buffer question this record left open does not arise for epoll:
readiness is the syscall-when-ready model, so no buffer is handed to a kernel
across a submit. That is the drive layer's existing stance -- it holds the
bytes to retransmit, not a view -- and epoll keeps it. A completion driver,
`io_uring` or IOCP, will still have to answer it; that is why the vtable was
shaped for completion from the first driver.

`--driver epoll --target python` is refused naming both, per the availability
rule; `--driver epoll` without `--layer drive` is refused too, since the
driver adds files over the drive layer rather than pulling it in. Held to a
socketpair by `test_epoll_driver.py`: the loop retransmits then correlates the
reply, and expires after the retry budget when nothing answers.

The "which to build" order above reasoned from the facilities and put
`blocking` and `poll` first; epoll was built first because it is the eighty
lines this record opened with, and the one the copyright holder asked for.

## Amendment, 2026-08-26: poll and blocking, the same shape

`--driver poll` and `--driver blocking` join epoll. They are the same
additive artifact and the same division of labour -- the loop owns the
socket, the clock and the timer; the state machine owns everything else and
reaches I/O only through the submit vtable -- differing only in how the loop
waits for the deadline `step` returns.

`poll` is the readiness sibling of epoll, POSIX rather than Linux and with no
`FD_SETSIZE` ceiling: it has no persistent registration, so the loop builds a
one-entry `struct pollfd` each turn and passes the wrap-safe deadline as the
`poll` timeout. Everything else -- the send-only submit, the recv-and-dispatch
block, `TRUNCATED`-means-done -- is epoll's, so it is the smallest real
multiplexer 0033 named.

`blocking` is the proof the machine drives with nothing beyond `read` and
`write`. With no multiplexer to wait on, the deadline becomes the socket's
`SO_RCVTIMEO`, reset each turn, so a blocking `recv` wakes on a datagram or
when the next retransmission is due, whichever is first. A zero difference is
floored to a poll rather than a block-forever. It is 0033's `blocking` case,
the one that needs no OS facility at all.

Both are C generators beside `epoll.py` -- `situc/codegen/c/poll.py` and
`blocking.py` -- gated on the same `driven()` test, refused on a non-C target
naming both and without `--layer drive`. Each is held to an AF_UNIX datagram
socketpair by its own test: retransmit-then-correlate, and expire after the
retry budget. (The three share the send-only submit, the `now_ms` clock and
the header boilerplate verbatim; a shared-helper refactor is the obvious
follow-up once `io_uring`'s completion loop shows what genuinely differs.)

## Amendment, 2026-08-26: io_uring, the shape proven not assumed

`--driver io_uring` is built, and it is the one that matters most to this
record: the vtable was made completion-shaped from the first driver on the
argument that a readiness loop implements completion trivially while the
reverse is impossible. Building a completion driver is what turns that
argument from a claim into a fact, and it held -- the same `situ_io_t` submit
vtable and the same `step`/`on_message` machine drive io_uring with no change
to the core, exactly as the readiness three do.

**Raw, not liburing.** The generated code speaks io_uring through
`<linux/io_uring.h>` and the two syscalls directly -- `io_uring_setup` and
`io_uring_enter` -- with the SQ/CQ rings `mmap`ed, so it depends only on the
kernel uapi header. That is what makes it self-contained and testable where
`liburing` is not installed. Ring indices are published release / read
acquire so the kernel never sees a half-written sqe; registered buffers are a
latency optimization deliberately not taken, ordinary buffers keeping the
code the shape a reader already knows.

**The completion mapping.** `submit` preps an `IORING_OP_SEND` and enters
fire-and-forget -- its completion is reaped and discarded, a lost send being
what the retry budget resends. The loop keeps exactly one `IORING_OP_RECV` in
flight and waits for a completion with a timeout equal to `step`'s next
deadline, carried as a `__kernel_timespec` through `io_uring_enter`'s
`IORING_ENTER_EXT_ARG` (kernel 5.11+; the box is 6.12). `ETIME` from the
enter is the timeout, and the loop re-`step`s to retransmit.

**The in-flight-buffer question, and why it still does not arise.** This is
the driver 0033 said would force that open question. It did not force a new
answer: the drive layer's existing stance -- an in-flight buffer is not
*viewed* -- holds under completion too. The recv buffer handed to the kernel
is not viewed until its cqe arrives, and the send bytes are the caller's
retransmit buffer, never viewed, so 12.3 has nothing to invalidate across a
submit. Registered buffers would sharpen it (they pin the memory in the
kernel for the ring's life, which is a rung-2 backing question); ordinary
buffers do not, which is the second reason this build uses them.

**One API difference, forced by the model.** A completion ring must exist
before the first `submit`, and the first submit happens inside `drive_send`,
before the loop. So the io_uring ctx carries the ring, and the caller runs
`situ_io_uring_setup(&ctx, fd)` before `_init`, then
`situ_io_uring_<relation>_run(&ctx, &drive, ...)`, then
`situ_io_uring_teardown(&ctx)` -- where the readiness drivers took a bare fd.
That is the ceremony 0033 said completion puts into the simple cases, living
in the driver where it was meant to, written once. The generator is
`situc/codegen/c/io_uring.py`; held to the same socketpair test as the
others.
