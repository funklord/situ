# 0033: drivers are a third axis, and the vtable is completion-shaped

Status: accepted. The taxonomy and the two shape calls are decided; which
drivers ship, and in what order, is not.
Date: 2026-08-08
Phase: 26.98 gains the deadline return; the drivers themselves are unscheduled

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
