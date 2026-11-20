// situ-edit-qt -- read and edit a message against a situ schema, in a window.
//
// The third frontend of decision 0034. It drives `situ-edit` and holds no
// editing rule, so the CLI can do everything this can; `--set`, `--set-bytes`
// and `--out` with `--no-ui` are the CLI's own flags, typed into the
// window's cells rather than into the CLI, which is how a test holds this
// frontend to the reference without a display.

#include "window.h"

#include <QApplication>
#include <QCommandLineParser>
#include <QTableWidget>

#include <cstdio>

namespace {

// Types `value` into the value cell of the row named `name`, which is
// exactly what a person does and runs the same handler.
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
	QApplication application(argc, argv);
	QApplication::setApplicationName(QStringLiteral("situ-edit-qt"));

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
	parser.addOptions({structure, offset, length, argument, hex, set,
	                   set_bytes, out, open_field, no_ui});
	parser.process(application);

	const QStringList positional = parser.positionalArguments();
	edit_window window;
	if (positional.size() != 2) {
		if (parser.isSet(no_ui) || !positional.isEmpty()) {
			std::fprintf(stderr, "situ-edit-qt: give a schema and a "
			             "message, or neither and use File > Open\n");
			return 1;
		}
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
		std::fprintf(stderr, "situ-edit-qt: %s\n",
		             qPrintable(window.error()));
		if (parser.isSet(no_ui))
			return 1;
	}

	// Down through the nested structs named, as a person would double
	// click their way there; the edits below land in the last.
	edit_window *here = &window;
	for (const QString &field : parser.values(open_field)) {
		here = here->open_field(field);
		if (here == nullptr) {
			std::fprintf(stderr, "situ-edit-qt: cannot open `%s`\n",
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
		std::fprintf(stderr, "situ-edit-qt: %s\n", qPrintable(why));
		if (parser.isSet(no_ui))
			return 1;
	}

	if (parser.isSet(no_ui)) {
		std::fputs(qPrintable(here->as_text()), stdout);
		if (parser.isSet(out) && !here->write_out())
			return 1;
		return 0;
	}
	window.show();
	return application.exec();
}
