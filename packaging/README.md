# Debian packaging

`make deb` builds two `.deb` files into `build/deb/`:

| package | arch | what is in it |
|---|---|---|
| `situc` | `all` | the compiler: `/usr/bin/situc`, the Python package under `/usr/lib/situc`, and the standard schemas under `/usr/share/situc/std` |
| `libsitu-dev` | the build's | the C runtime: `/usr/include/situ.h` and `/usr/lib/libsitu.a` |

**Two packages rather than one, because they do not have the same
architecture.** `situc` is Python and runs anywhere; `libsitu.a` is compiled
objects and does not. One package would have to claim the narrower of the two,
which would make the schema compiler uninstallable on any machine it was not
built on -- for a tool whose whole point is generating code for targets other
than the build host, that is the wrong way round.

They are independent: a build machine needs `situc` and no runtime, and a
target needs the runtime headers and no compiler. Neither depends on the
other, and `situc` needs nothing but `python3`.

## How it is built

`dpkg-buildpackage` driving `debian/rules`, which is three lines of `dh`.

**It was `dpkg-deb --build` over a tree staged by
`make install DESTDIR=...`**, and this section described that until the two
were read against each other. The debhelper packaging replaced it in
`d943802`, which made both packages byte-identical in content; the two
`.control` files this directory carried for the old build went with it, and
are in the history at `85535fb` if that shape is ever wanted again.

One of the two reasons for the old approach survives the change.
`dh_auto_install` calls `make install DESTDIR=...`, so the packages still
contain exactly what the install rule produces, and a packaging bug and an
install bug still cannot be different bugs.

**The other was never answered, and is left here rather than dropped.** The
old build needed only `dpkg-deb`, because section 24 asks the toolchain to
vendor into an embedded build environment where `pip install` is not on the
table, and a packaging step wanting `debhelper` is the same problem wearing
a different hat. The migration did not say what becomes of that constraint.
Whether it still binds is a real question and not one to answer by noticing
it.

The version comes from `VERSION` at the root -- the same string `situc
--version` prints -- so there is no second place to forget. It was
`situc/__init__.py`, which now reads that file.
