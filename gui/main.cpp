// situ-edit-gui -- read and edit a message against a situ schema, in a
// window or, built with qtty, the same window on a terminal (`--tui`).
//
// The third frontend of decision 0034. It drives `situ-edit` and holds no
// editing rule, and since 26.1113 no display rule either: every cell is the
// `view` the CLI's JSON carries, which the text table and the curses TUI
// draw too. `--set`, `--set-bytes` and `--out` with `--no-ui` are the CLI's
// own flags, typed into the window's cells rather than into the CLI, which
// is how a test holds this frontend to the reference without a display.
//
// One QWidget codebase for the window and the terminal, which is qtty's
// premise. The terminal frontend that needs nothing but Python is
// `situ-edit-tui`; this one needs Qt, on a terminal as in a window.

#include "desktop_scheme.h"
#include "window.h"

#include <QApplication>
#include <QCommandLineParser>
#include <QFileInfo>
#include <QIcon>
#include <QComboBox>
#include <QKeyEvent>
#include <QLineEdit>
#include <QTableWidget>
#include <QTimer>

#include <cstdio>
#include <cstring>
#include <memory>
#include <unistd.h>

#ifdef SITU_QTTY
#include <qtty/qtty.h>
#endif

#ifndef SITU_VERSION
#define SITU_VERSION "unknown"
#endif

namespace {

// Whether the terminal was asked for: decided before QApplication exists,
// since qtty chooses the platform Qt reads.
bool asked_for_tui(int argc, char **argv)
{
	for (int i = 1; i < argc; i++) {
		if (std::strcmp(argv[i], "--tui") == 0)
			return true;
		if (std::strcmp(argv[i], "--gui") == 0)
			return false;
	}
#ifdef SITU_QTTY
	// No display, on a terminal: the terminal is what there is.
	return !qEnvironmentVariableIsSet("WAYLAND_DISPLAY") &&
	       !qEnvironmentVariableIsSet("DISPLAY") && isatty(1) &&
	       !qEnvironmentVariableIsSet("QT_QPA_PLATFORM");
#else
	return false;
#endif
}

// Types `value` into the value cell of the row named `name`, which is
// exactly what a person does and runs the same handler.
// `--keys FIELD=SPEC`: open the value cell's editor -- the widget a person
// gets, validator, completer and all -- and press keys into it. Text is
// typed a character at a time; `{Return}`, `{Escape}`, `{Down}`, `{Up}` and
// `{Tab}` are those keys. No Return is added: a spec without one leaves the
// editor open, which is a case worth testing.
//
// Two deliveries, because the terminal's is new and is the one to doubt.
// Without qtty a key goes where Qt sends a real one -- the active popup (a
// completer's list) first, else the focused widget. With --tui it goes
// into qtty's InputRouter, exactly as bytes from a terminal arrive.
struct key { int code; QString text; };

QList<key> keys_of(const QString &spec)
{
	static const QHash<QString, int> named = {
		{QStringLiteral("Return"), Qt::Key_Return},
		{QStringLiteral("Escape"), Qt::Key_Escape},
		{QStringLiteral("Down"), Qt::Key_Down},
		{QStringLiteral("Up"), Qt::Key_Up},
		{QStringLiteral("Tab"), Qt::Key_Tab},
	};
	QList<key> keys;
	for (qsizetype at = 0; at < spec.size(); ++at) {
		if (spec[at] == QLatin1Char('{')) {
			const qsizetype end = spec.indexOf(QLatin1Char('}'), at);
			const QString name = spec.mid(at + 1, end - at - 1);
			if (end > at && named.contains(name)) {
				keys.append({named.value(name), QString()});
				at = end;
				continue;
			}
		}
		const QChar c = spec[at];
		const int code = c.isLetterOrNumber() ? int(c.toUpper().unicode())
		                 : c == QLatin1Char(' ') ? int(Qt::Key_Space) : 0;
		keys.append({code, QString(c)});
	}
	return keys;
}

void press_by_qt(const key &one)
{
	QWidget *to = QApplication::activePopupWidget();
	if (to == nullptr)
		to = QApplication::focusWidget();
	if (to == nullptr)
		return;
	QKeyEvent down(QEvent::KeyPress, one.code, Qt::NoModifier, one.text);
	QApplication::sendEvent(to, &down);
	QKeyEvent up(QEvent::KeyRelease, one.code, Qt::NoModifier, one.text);
	QApplication::sendEvent(to, &up);
}

bool open_editor(edit_window &window, const QString &name, QString *why)
{
	auto *table = window.findChild<QTableWidget *>();
	for (int row = 0; table && row < table->rowCount(); ++row) {
		if (table->item(row, 2)->text() != name)
			continue;
		QTableWidgetItem *cell = table->item(row, 3);
		if (!(cell->flags() & Qt::ItemIsEditable)) {
			*why = QStringLiteral("`%1` is not writable here").arg(name);
			return false;
		}
		table->setFocus();
		table->setCurrentCell(row, 3);
		table->editItem(cell);
		QCoreApplication::processEvents();
		return true;
	}
	*why = QStringLiteral("no field `%1`").arg(name);
	return false;
}

bool type_into(edit_window &window, const QString &name, const QString &value,
               QString *why)
{
	auto *table = window.findChild<QTableWidget *>();
	for (int row = 0; table && row < table->rowCount(); ++row) {
		if (table->item(row, 2)->text() != name)
			continue;
		QTableWidgetItem *cell = table->item(row, 3);
		if (!(cell->flags() & Qt::ItemIsEditable)) {
			*why = QStringLiteral("`%1` is not writable here").arg(name);
			return false;
		}
		cell->setText(value);
		const QString said = window.status_text();
		if (said.startsWith(QStringLiteral("refused: "))) {
			*why = said.mid(9);
			return false;
		}
		return true;
	}
	*why = QStringLiteral("no field `%1`").arg(name);
	return false;
}

}

int main(int argc, char **argv)
{
	if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
		// The first line keeps its shape for whatever parses it; who
		// wrote it is a line of its own.
		std::printf("situ-edit-gui %s\n%s\n", SITU_VERSION, SITU_COPYRIGHT);
		return 0;
	}
	const bool tui = asked_for_tui(argc, argv);
#ifdef SITU_QTTY
	if (tui)
		Qtty::prepare_environment();
#else
	if (tui) {
		std::fprintf(stderr, "situ-edit-gui: this build has no terminal "
		             "frontend: it is compiled in where qtty is "
		             "(vendor/qtty, `make gui` fetches it); situ-edit-tui "
		             "is the terminal frontend that needs only Python\n");
		return 2;
	}
#endif
	QApplication application(argc, argv);
#ifdef SITU_QTTY
	// A terminal's habits: Enter on the focused control, Up and Down
	// between controls, Alt+letter for the menu. The table binds none of
	// those itself.
	if (tui) {
		Qtty::setup(application);
		Qtty::set_keyboard_conventions(true);
	}
#endif
	// A dark desktop gets a dark window (desktop_scheme.h). Not on a
	// terminal: there the terminal's own colours are what qtty draws with.
	if (!tui)
		follow_desktop(application);
	QApplication::setApplicationName(QStringLiteral("situ-edit-gui"));
	// The launcher and the running window as one entry: X11 matches
	// WM_CLASS against the .desktop's StartupWMClass, Wayland this name.
	QGuiApplication::setDesktopFileName(QStringLiteral("situ-edit-gui"));
	QApplication::setWindowIcon(QIcon::fromTheme(QStringLiteral("situ-edit-gui")));
	QApplication::setApplicationVersion(QStringLiteral(SITU_VERSION));

	QCommandLineParser parser;
	parser.setApplicationDescription(
	    QStringLiteral("read and edit a message against a situ schema"));
	parser.addHelpOption();
	parser.addPositionalArgument(QStringLiteral("schema"),
	    QStringLiteral("a .situ schema, or a packed image"));
	parser.addPositionalArgument(QStringLiteral("message"),
	    QStringLiteral("the bytes to read"));
	const QCommandLineOption structure(QStringLiteral("struct"),
	    QStringLiteral("which layout to read it as"), QStringLiteral("NAME"));
	const QCommandLineOption offset(QStringLiteral("offset"),
	    QStringLiteral("where the struct starts in the file"),
	    QStringLiteral("N"));
	const QCommandLineOption length(QStringLiteral("length"),
	    QStringLiteral("how long its frame is"), QStringLiteral("N"));
	const QCommandLineOption argument(QStringLiteral("arg"),
	    QStringLiteral("a value for a `parameter` member, in order"),
	    QStringLiteral("N"));
	const QCommandLineOption hex(QStringLiteral("hex"),
	    QStringLiteral("the message file holds hex rather than bytes"));
	const QCommandLineOption set(QStringLiteral("set"),
	    QStringLiteral("store a value, as the CLI's --set"),
	    QStringLiteral("NAME=VALUE"));
	const QCommandLineOption set_bytes(QStringLiteral("set-bytes"),
	    QStringLiteral("store a byte run, as the CLI's --set-bytes"),
	    QStringLiteral("NAME=HEX"));
	const QCommandLineOption out(QStringLiteral("out"),
	    QStringLiteral("where Save writes; the message opened is never "
	                   "overwritten unless it is named here"),
	    QStringLiteral("FILE"));
	const QCommandLineOption open_field(QStringLiteral("open"),
	    QStringLiteral("open the nested struct in this field in its own "
	                   "window, as a double click does; repeat to go deeper, "
	                   "and --set applies in the last"),
	    QStringLiteral("FIELD"));
	const QCommandLineOption no_ui(QStringLiteral("no-ui"),
	    QStringLiteral("apply --set/--set-bytes through the window, print "
	                   "the table, write --out, and exit"));
	const QCommandLineOption keys_option(QStringLiteral("keys"),
	    QStringLiteral("open FIELD's editor and press keys into it, through "
	                   "qtty's input with --tui: text, and {Return}, "
	                   "{Escape}, {Down}, {Up}, {Tab}; after --set"),
	    QStringLiteral("FIELD=SPEC"));
	const QCommandLineOption tui_option(QStringLiteral("tui"),
	    QStringLiteral("the same window on this terminal, drawn by qtty"));
	const QCommandLineOption gui_option(QStringLiteral("gui"),
	    QStringLiteral("a window, even with no display in the environment"));
	const QCommandLineOption snapshot(QStringLiteral("snapshot"),
	    QStringLiteral("with --tui: print the window as the terminal shows "
	                   "it, COLSxROWS, after --open and --set, and exit"),
	    QStringLiteral("COLSxROWS"));
	const QCommandLineOption scheme_option(QStringLiteral("scheme"),
	    QStringLiteral("print whether the desktop reads as dark, light or "
	                   "unknown -- what the window follows -- and exit"));
	parser.addOption(scheme_option);
	parser.addOptions({structure, offset, length, argument, hex, set,
	                   set_bytes, out, open_field, no_ui, keys_option,
	                   tui_option,
	                   gui_option, snapshot});
	parser.process(application);
	if (parser.isSet(scheme_option)) {
		std::printf("%s\n", qPrintable(scheme_name(current_scheme())));
		return 0;
	}

	const QStringList positional = parser.positionalArguments();
	edit_window window;
	if (positional.size() != 2) {
		if (parser.isSet(no_ui) || !positional.isEmpty()) {
			std::fprintf(stderr, "situ-edit-gui: give a schema and a "
			             "message, or neither and use File > Open\n");
			return 1;
		}
#ifdef SITU_QTTY
		if (tui)
			return Qtty::exec(application, window);
#endif
		window.show();
		return application.exec();
	}

	open_options options;
	options.schema    = positional[0];
	options.message   = positional[1];
	options.structure = parser.value(structure);
	options.offset    = parser.value(offset).toLongLong();
	options.length    = parser.isSet(length)
	                    ? parser.value(length).toLongLong() : -1;
	options.hex       = parser.isSet(hex);
	options.arguments = parser.values(argument);
	if (!window.open(options, parser.value(out))) {
		std::fprintf(stderr, "situ-edit-gui: %s\n",
		             qPrintable(window.error()));
		if (parser.isSet(no_ui) || parser.isSet(snapshot))
			return 1;
	}

	// Down through the nested structs named, as a person would double
	// click their way there; the edits below land in the last.
	edit_window *here = &window;
	for (const QString &field : parser.values(open_field)) {
		here = here->open_field(field);
		if (here == nullptr) {
			std::fprintf(stderr, "situ-edit-gui: cannot open `%s`\n",
			             qPrintable(field));
			return 1;
		}
	}

	// The CLI applies every --set, then every --set-bytes; so does this.
	QString why;
	const auto apply = [&](const QStringList &assignments) {
		for (const QString &assignment : assignments) {
			const qsizetype equals = assignment.indexOf(QLatin1Char('='));
			if (equals < 0) {
				why = QStringLiteral("`%1` is not `name=value`")
				      .arg(assignment);
				return false;
			}
			if (!type_into(*here, assignment.left(equals).trimmed(),
			               assignment.mid(equals + 1), &why))
				return false;
		}
		return true;
	};
	if (!apply(parser.values(set)) || !apply(parser.values(set_bytes))) {
		std::fprintf(stderr, "situ-edit-gui: %s\n", qPrintable(why));
		if (parser.isSet(no_ui) || parser.isSet(snapshot))
			return 1;
	}

#ifdef SITU_QTTY
	// qtty's input, for --keys, kept for --snapshot: the screen it composes
	// is the window AND what is open over it -- a completer's list, the
	// caret -- which the window alone does not draw.
	std::unique_ptr<Qtty::InputRouter> router;
#endif
	if (parser.isSet(keys_option)) {
#ifdef SITU_QTTY
		// At the size the snapshot will be taken at, BEFORE the keys: a
		// list a key opens is placed against the window as it stands, and
		// a resize after it measured qtty's relayout rather than what a
		// terminal of that size shows (qtty 8.787).
		if (tui && parser.isSet(snapshot)) {
			const QStringList size = parser.value(snapshot)
			                         .split(QLatin1Char('x'));
			here->resize(Qtty::GridMetrics::cells(size.value(0).toInt(),
			                                      size.value(1).toInt()));
		}
#endif
		here->show();
		here->activateWindow();
		QCoreApplication::processEvents();
#ifdef SITU_QTTY
		if (tui)
			router = std::make_unique<Qtty::InputRouter>(here);
#endif
		for (const QString &typed : parser.values(keys_option)) {
			const qsizetype equals = typed.indexOf(QLatin1Char('='));
			if (equals < 0 || !open_editor(*here, typed.left(equals), &why)) {
				std::fprintf(stderr, "situ-edit-gui: %s\n", equals < 0
				             ? "--keys takes FIELD=SPEC" : qPrintable(why));
				return 1;
			}
			for (const key &one : keys_of(typed.mid(equals + 1))) {
#ifdef SITU_QTTY
				if (router) {
					router->on_key({one.code, one.text, false, false,
					                false});
					QCoreApplication::processEvents();
					continue;
				}
#endif
				press_by_qt(one);
				QCoreApplication::processEvents();
			}
			// What the editor holds when the keys run out, so a test can
			// tell "refused at the key" from "typed and not committed".
			QWidget *focused = QApplication::focusWidget();
			if (auto *line = qobject_cast<QLineEdit *>(focused))
				std::printf("editor: %s\n", qPrintable(line->text()));
			else if (auto *box = qobject_cast<QComboBox *>(focused))
				std::printf("editor: %s\n",
				            qPrintable(box->currentText()));
		}
	}

	if (parser.isSet(snapshot)) {
#ifdef SITU_QTTY
		const QStringList size = parser.value(snapshot)
		                         .split(QLatin1Char('x'));
		const int cols = size.value(0).toInt();
		const int rows = size.value(1).toInt();
		if (!tui || cols < 20 || rows < 5) {
			std::fprintf(stderr, "situ-edit-gui: --snapshot needs --tui "
			             "and a size such as 100x30\n");
			return 2;
		}
		// The window the edits landed in, which is the one on top.
		here->resize(Qtty::GridMetrics::cells(cols, rows));
		here->show();
		QTimer::singleShot(0, here, [here, cols, rows, &router] {
			const QString shown = router
			    ? Qtty::test::snapshot_of_screen(*here, *router, cols, rows)
			    : Qtty::test::snapshot_of(*here, cols, rows);
			std::fputs(shown.toUtf8().constData(), stdout);
			std::fflush(stdout);
			QCoreApplication::exit(0);
		});
		return application.exec();
#else
		std::fprintf(stderr, "situ-edit-gui: --snapshot needs a build "
		             "with qtty\n");
		return 2;
#endif
	}

	if (parser.isSet(no_ui)) {
		std::fputs(qPrintable(here->as_text()), stdout);
		if (parser.isSet(out) && !here->write_out())
			return 1;
		return 0;
	}
#ifdef SITU_QTTY
	if (tui)
		return Qtty::exec(application, window);
#endif
	window.show();
	return application.exec();
}
