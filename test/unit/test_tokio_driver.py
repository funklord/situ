"""The tokio driver, held to a real tokio runtime over real sockets (0033).

`--driver tokio` is the second host runtime and the first Rust cell. It is
the other resolution of the problem the Qt driver posed: Qt's loop belongs to
the application, so that driver installs a notifier and returns and the
outcome arrives at a callback; an `async fn` *is* the awaitable, so this one
returns the outcome and the caller `.await`s it inside the runtime it
already has.

The behavioural test builds a real cargo project against tokio and drives
exchanges over two UDP sockets on loopback, with an in-process peer that
drops, alters or garbles datagrams, and asks for every hook the driver calls
(26.775). It skips where cargo is absent or the tokio crate cannot be
fetched -- and only then: a driver that does not compile fails.

The exchange it drives is keyed on a plain integer rather than on `dns`'s
`opcode`. That is not an arbitrary choice: the Rust `relate`, `converse` and
`drive` layers do not compile for a relation keyed on an enum-typed field --
see `test_the_rust_key_does_not_compile_for_an_enum_field` below, which pins
the defect rather than working around it silently.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

CARGO   = shutil.which("cargo")
RUNTIME = ROOT / "runtime" / "rust" / "situ_rt.rs"

#: An exchange whose key is a plain integer. `dns`'s `reply_to` keys on
#: `opcode`, which the Rust backend cannot compile (see the pinned defect at
#: the foot of this file), so the driver is exercised on a schema that does
#: reach the compiler.
SCHEMA = """target buffer;
endian big;

struct echo_msg {
\tu16  id;
\tu16  seq;
}

relation reply_to(query: echo_msg, reply: echo_msg)
\t\t[timeout_ms = 150, retries = 2] {
\tmust reply.id == query.id;
}
"""

CARGO_TOML = """[package]
name = "tokv"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }

[[bin]]
name = "tokv"
path = "src/main.rs"
"""

#: Drives the generated artifact through every hook it calls -- the cases
#: `c_driver_harness` puts to the C drivers (26.775) but the oversized
#: datagram, which UDP cannot carry: a reply after two dropped datagrams,
#: read through its view, an expiry, an unmatched reply, a short datagram,
#: and `stop` from a hook, each with `on_done` exactly once.
MAIN = r"""
mod situ_rt;
mod echo;
mod echo_relate;
mod echo_frame;
mod echo_converse;
mod echo_drive;
mod echo_tokio;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

const ECHO: u32 = 0;
const SILENT: u32 = 1;
const WRONG_ID: u32 = 2;
const SHORT_FIRST: u32 = 3;
const ONLY_GARBAGE: u32 = 4;

async fn peer(socket: Arc<tokio::net::UdpSocket>, seen: Arc<AtomicU32>,
              mode: u32, drop_first: u32) {
	let mut buf = [0u8; 2048];
	loop {
		let (n, from) = match socket.recv_from(&mut buf).await {
			Ok(pair) => pair,
			Err(_) => return,
		};
		let count = seen.fetch_add(1, Ordering::SeqCst) + 1;
		if mode == SILENT || count <= drop_first {
			continue;
		}
		if mode == WRONG_ID {
			buf[1] = buf[1].wrapping_add(1);
		}
		if mode == SHORT_FIRST && count == drop_first + 1 {
			let _ = socket.send_to(&buf[..1], from).await;
		}
		if mode == ONLY_GARBAGE {
			let _ = socket.send_to(&buf[..1], from).await;
			continue;
		}
		let _ = socket.send_to(&buf[..n], from).await;
	}
}

#[derive(Default)]
struct Recorder {
	replies: u32,
	id: u32,
	view_id: u16,
	expired: u32,
	unmatched: u32,
	malformed: u32,
	errors: u32,
	done: u32,
	stop_on_malformed: bool,
	stopped: bool,
}

impl echo_tokio::ReplyToHooks for Recorder {
	fn on_reply(&mut self, id: u32, reply: &echo::EchoMsg<'_>) {
		self.replies += 1;
		self.id = id;
		self.view_id = reply.id();
	}
	fn on_unmatched(&mut self, _: &echo::EchoMsg<'_>, _: situ_rt::Error) {
		self.unmatched += 1;
	}
	fn on_malformed(&mut self, _: &[u8], _: situ_rt::Error) {
		self.malformed += 1;
		if self.stop_on_malformed {
			self.stopped = true;
		}
	}
	fn on_expired(&mut self, count: u32) {
		self.expired += count;
	}
	fn on_error(&mut self, _: situ_rt::Error, _: Option<&std::io::Error>) {
		self.errors += 1;
	}
	fn on_done(&mut self, _: situ_rt::Result<()>) {
		self.done += 1;
	}
	fn stop(&self) -> bool {
		self.stopped
	}
}

async fn case(name: &str, mode: u32, drop_first: u32,
              stop_on_malformed: bool) -> (Recorder, u32, bool) {
	let peer_sock = Arc::new(tokio::net::UdpSocket::bind("127.0.0.1:0")
		.await.expect("peer bind"));
	let peer_addr = peer_sock.local_addr().expect("peer addr");

	// Bind as a std socket so the sink can keep a handle that performs the
	// real send(2): tokio's try_send reports WouldBlock from its own
	// readiness, which costs a retransmission and its retry budget together.
	let std_sock = std::net::UdpSocket::bind("127.0.0.1:0").expect("our bind");
	std_sock.connect(peer_addr).expect("connect");
	let send_on = Arc::new(std_sock.try_clone().expect("clone for sending"));
	std_sock.set_nonblocking(true).expect("nonblocking for tokio");
	let ours = Arc::new(tokio::net::UdpSocket::from_std(std_sock)
		.expect("adopt into tokio"));

	let seen = Arc::new(AtomicU32::new(0));
	let peer_task = tokio::spawn(peer(peer_sock.clone(), seen.clone(), mode,
	                                  drop_first));

	let request: [u8; 4] = [0x4e, 0x4f, 0x00, 0x01];
	let query = echo::EchoMsg::new(&request).expect("query view");
	let sink = echo_tokio::TokioIo::new(send_on.clone());
	let mut store = [echo_drive::ReplyToSlot::default(); 4];
	let mut drive = echo_drive::ReplyToDriver::new(&mut store, sink, 150, 2);
	let base = std::time::Instant::now();
	drive.send(&query, &request, 0x1234, echo_tokio::now_ms(base))
		.expect("send");

	let mut hooks = Recorder { stop_on_malformed, ..Recorder::default() };
	// A watchdog, so a loop that never completes fails rather than hangs.
	let outcome = tokio::time::timeout(
		std::time::Duration::from_secs(5),
		echo_tokio::run_reply_to(&ours, &mut drive, base, &mut hooks),
	).await;
	peer_task.abort();
	let late = !matches!(outcome, Ok(Ok(())));
	if late {
		println!("FAIL {}: did not complete cleanly", name);
	}
	(hooks, seen.load(Ordering::SeqCst), late)
}

fn check(name: &str, ok: bool, r: &Recorder) -> bool {
	if !ok {
		println!("FAIL {}: replies={} view_id=0x{:x} expired={} unmatched={} \
		          malformed={} errors={} done={}", name, r.replies, r.view_id,
		         r.expired, r.unmatched, r.malformed, r.errors, r.done);
	}
	ok
}

#[tokio::main]
async fn main() {
	let mut ok = true;
	let (r, seen, late) = case("retransmit", ECHO, 2, false).await;
	ok &= check("retransmit", !late && r.replies == 1 && r.id == 0x1234
	            && r.view_id == 0x4e4f && seen == 3 && r.done == 1
	            && r.errors == 0, &r);
	let (r, _, late) = case("expire", SILENT, 0, false).await;
	ok &= check("expire", !late && r.replies == 0 && r.expired == 1
	            && r.done == 1, &r);
	let (r, _, late) = case("unmatched", WRONG_ID, 0, false).await;
	ok &= check("unmatched", !late && r.unmatched >= 1 && r.expired == 1, &r);
	let (r, _, late) = case("short", SHORT_FIRST, 0, false).await;
	ok &= check("short", !late && r.malformed == 1 && r.replies == 1, &r);
	let (r, _, late) = case("stop", ONLY_GARBAGE, 0, true).await;
	ok &= check("stop", !late && r.malformed == 1 && r.replies == 0
	            && r.expired == 0 && r.done == 1, &r);
	if ok {
		println!("ALL PASS");
	}
}
"""


def _cargo_project(tmp_path: Path) -> Path | None:
	"""Generate the driver into a cargo project, or None where the crate
	cannot be built -- an offline machine skips rather than fails."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "rust", "--layer", "drive", "--driver", "tokio",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert (gen / "echo_tokio.rs").exists(), built.stderr

	proj = tmp_path / "proj"
	(proj / "src").mkdir(parents=True)
	(proj / "Cargo.toml").write_text(CARGO_TOML, encoding="ascii")
	for part in gen.glob("*.rs"):
		(proj / "src" / part.name).write_text(
			part.read_text(encoding="ascii"), encoding="ascii")
	# The runtime carries `#![no_std]`, which is a crate-root attribute and
	# cannot travel in a module.
	(proj / "src" / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(proj / "src" / "main.rs").write_text(MAIN, encoding="ascii")
	return proj


#: What cargo says when the crate cannot be had, as opposed to when the
#: generated code does not compile. Skipping on any build failure let a
#: driver that no longer compiled read as an offline machine.
UNFETCHABLE = ("failed to download", "Unable to update registry",
               "failed to get `tokio`", "no matching package",
               "Couldn't resolve host", "failed to load source")


@pytest.mark.skipif(CARGO is None, reason="no cargo")
def test_the_tokio_driver_calls_every_hook(tmp_path: Path) -> None:
	"""Every hook the driver calls, over UDP on loopback. Watched failing
	with the deadline arm removed from the generated `select!`: the watchdog
	fires, because nothing ever resends."""
	proj = _cargo_project(tmp_path)
	assert proj is not None and CARGO is not None

	compiled = subprocess.run([CARGO, "build", "--quiet"], cwd=proj,
	                          capture_output=True, text=True, timeout=900)
	if compiled.returncode != 0 and any(
			word in compiled.stderr for word in UNFETCHABLE):
		pytest.skip(f"the tokio crate is unavailable here: {compiled.stderr[:200]}")
	assert compiled.returncode == 0, compiled.stderr[-3000:]

	ran = subprocess.run([CARGO, "run", "--quiet"], cwd=proj,
	                     capture_output=True, text=True, timeout=300)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "FAIL" not in ran.stdout, ran.stdout
	assert "ALL PASS" in ran.stdout, ran.stdout


def test_tokio_is_refused_on_c(tmp_path: Path) -> None:
	"""A driver crosses the backend axis, and the refusal names both."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "c", "--layer", "drive", "--driver", "tokio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "tokio" in refused.stderr and "rust" in refused.stderr


def test_tokio_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a module over the drive layer rather than pulling it
	in, so it must be asked for alongside `--layer drive`."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "rust", "--driver", "tokio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "tokio" in refused.stderr and "drive" in refused.stderr


# -- the two defects this driver found, both fixed -------------------------
#
# Neither is pinned here any more, and each left by the same door. Rust's
# `step` dropped the expiry count on the call that gives up on the last
# exchange; its regression test lives beside C's and Python's in
# `test_relations.py`, which is where a claim about all three backends
# belongs. The Rust key builders cast an enum field's `Option<Enum>`, so a
# relation keyed on one did not compile at all; that is a claim about Rust
# codegen and its tests live in `test_codegen_rust.py`. Both pins flipped to
# XPASS the moment their fix landed, which is what `strict=True` is for.
