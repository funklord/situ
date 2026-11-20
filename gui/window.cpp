// window.cpp -- see window.h.

#include "window.h"

#include <QCloseEvent>
#include <QFileDialog>
#include <QFileInfo>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QMenuBar>
#include <QMessageBox>
#include <QStatusBar>
#include <QTableWidget>
#include <QVBoxLayout>

namespace {

enum column { offset_column, size_column, name_column, value_column,
              write_column, note_column, column_count };

// Non-native dialogs: a native one is a separate process qtty cannot draw,
// and on a terminal it would never appear.
constexpr QFileDialog::Options dialog_options =
    QFileDialog::DontUseNativeDialog;

QString shown_value(const QJsonObject &field)
{
	if (field.value(QStringLiteral("withheld")).toBool()) {
		// `[secret]`: the length and not the bytes, as the CLI's table and
		// its JSON both do (26.586) -- here the JSON has already withheld
		// them, and this says why the cell is empty.
		return QStringLiteral("<withheld: secret>");
	}
	const QJsonValue value = field.value(QStringLiteral("value"));
	if (value.isNull())
		return QStringLiteral("(%1)").arg(
		    field.value(QStringLiteral("note")).toString());
	if (value.isString())
		return value.toString();
	return QString::number(value.toInteger());
}

QString number_or_dash(const QJsonValue &value)
{
	return value.isNull() ? QStringLiteral("--")
	                      : QString::number(value.toInteger());
}

}

edit_window::edit_window(QWidget *parent)
    : QMainWindow(parent)
{
	auto *centre = new QWidget(this);
	auto *layout = new QVBoxLayout(centre);
	header_ = new QLabel(centre);
	header_->setTextInteractionFlags(Qt::TextSelectableByMouse);
	header_->setWordWrap(true);
	table_ = new QTableWidget(0, column_count, centre);
	table_->setHorizontalHeaderLabels({tr("Offset"), tr("Size"), tr("Field"),
	                                   tr("Value"), tr("Write"), tr("Note")});
	table_->verticalHeader()->hide();
	table_->horizontalHeader()->setStretchLastSection(true);
	table_->setSelectionBehavior(QAbstractItemView::SelectRows);
	table_->setSelectionMode(QAbstractItemView::SingleSelection);
	table_->setEditTriggers(QAbstractItemView::DoubleClicked |
	                        QAbstractItemView::EditKeyPressed |
	                        QAbstractItemView::AnyKeyPressed);
	layout->addWidget(header_);
	layout->addWidget(table_);
	setCentralWidget(centre);

	QMenu *file = menuBar()->addMenu(tr("&File"));
	file->addAction(tr("&Open..."), QKeySequence::Open,
	                this, &edit_window::open_dialog);
	file->addAction(tr("&Save"), QKeySequence::Save,
	                this, [this] { save(); });
	file->addAction(tr("Save &As..."), QKeySequence::SaveAs,
	                this, [this] { save_as(); });
	file->addSeparator();
	file->addAction(tr("&Quit"), QKeySequence::Quit, this, &QWidget::close);

	connect(table_, &QTableWidget::itemChanged, this, &edit_window::edited);
	setWindowTitle(QStringLiteral("situ-edit-qt"));
	resize(820, 560);
}

bool edit_window::open(const open_options &options, const QString &out)
{
	out_ = out;
	if (!session_.open(options)) {
		say(session_.error());
		return false;
	}
	setWindowTitle(QStringLiteral("%1 -- %2 -- situ-edit-qt")
	               .arg(QFileInfo(options.message).fileName(),
	                    QFileInfo(options.schema).fileName()));
	populate();
	say(tr("Edit a value to set it; the schema decides what may be "
	       "written."));
	return true;
}

void edit_window::populate()
{
	const QJsonObject &document = session_.document();

	// THE HEADER, as the CLI's first line: the struct, how much of the
	// window it measures, and the verdict, which qualifies every row.
	QString heading = QStringLiteral("<b>%1</b>  ").arg(
	    document.value(QStringLiteral("struct")).toString().toHtmlEscaped());
	const qint64 bytes = document.value(QStringLiteral("bytes")).toInteger();
	const QJsonValue measured = document.value(QStringLiteral("measured"));
	if (measured.isNull())
		heading += tr("%1 bytes, extent unknown").arg(bytes);
	else if (measured.toInteger() < bytes)
		heading += tr("%1 of %2 bytes").arg(measured.toInteger()).arg(bytes);
	else
		heading += tr("%1 bytes").arg(bytes);
	const qint64 at = document.value(QStringLiteral("at")).toInteger();
	if (at != 0)
		heading += tr(" at %1").arg(at);
	const QJsonObject verdict =
	    document.value(QStringLiteral("verdict")).toObject();
	if (!verdict.isEmpty()) {
		const QString word = verdict.value(QStringLiteral("word")).toString();
		heading += QStringLiteral("  [%1]").arg(word.toHtmlEscaped());
		if (word != QStringLiteral("ok"))
			heading += QStringLiteral("<br>validate: %1").arg(
			    verdict.value(QStringLiteral("why")).toString()
			    .toHtmlEscaped());
	}
	for (const QJsonValue &said :
	     document.value(QStringLiteral("messages")).toArray()) {
		const QJsonObject message = said.toObject();
		heading += QStringLiteral("<br>%1: %2 -- %3").arg(
		    message.value(QStringLiteral("severity")).toString(),
		    message.value(QStringLiteral("name")).toString(),
		    message.value(QStringLiteral("text")).toString()
		    .toHtmlEscaped());
	}
	header_->setText(heading);

	const QJsonArray fields = document.value(QStringLiteral("fields")).toArray();
	filling_ = true;
	table_->setRowCount(int(fields.size()));
	for (int row = 0; row < fields.size(); ++row) {
		const QJsonObject field = fields[row].toObject();
		const bool can_write =
		    field.value(QStringLiteral("writable")).toBool() &&
		    field.value(QStringLiteral("readable")).toBool() &&
		    !field.value(QStringLiteral("withheld")).toBool();

		auto fixed = [](const QString &text) {
			auto *item = new QTableWidgetItem(text);
			item->setFlags(Qt::ItemIsSelectable | Qt::ItemIsEnabled);
			return item;
		};
		table_->setItem(row, offset_column, fixed(number_or_dash(
		    field.value(QStringLiteral("offset")))));
		table_->setItem(row, size_column, fixed(number_or_dash(
		    field.value(QStringLiteral("size")))));
		table_->setItem(row, name_column, fixed(
		    field.value(QStringLiteral("name")).toString()));

		auto *value = new QTableWidgetItem(shown_value(field));
		Qt::ItemFlags flags = Qt::ItemIsSelectable | Qt::ItemIsEnabled;
		if (can_write)
			flags |= Qt::ItemIsEditable;
		value->setFlags(flags);
		// Which flag the CLI is asked with is decided by what the field
		// holds, as in the TUI: a byte run is hex, anything else a number.
		value->setData(Qt::UserRole,
		               field.value(QStringLiteral("kind")).toString() ==
		               QStringLiteral("bytes"));
		table_->setItem(row, value_column, value);

		// What a write would do, from the image's capability vector -- the
		// part no template-driven hex editor can show (0034).
		const QJsonValue cost = field.value(QStringLiteral("write_cost"));
		auto *write = fixed(can_write ? cost.toString()
		                              : tr("not writable"));
		write->setToolTip(QStringLiteral("mutate: %1\nauth: %2\n"
		                                 "secrecy: %3\neffect: %4").arg(
		    field.value(QStringLiteral("mutate")).toString(),
		    field.value(QStringLiteral("auth")).toString(),
		    field.value(QStringLiteral("secrecy")).toString(),
		    field.value(QStringLiteral("effect")).toString()));
		table_->setItem(row, write_column, write);
		const QJsonValue note = field.value(QStringLiteral("note"));
		table_->setItem(row, note_column, fixed(
		    field.value(QStringLiteral("value")).isNull()
		    ? QString() : note.toString()));
	}
	table_->resizeColumnsToContents();
	filling_ = false;
}

void edit_window::edited(QTableWidgetItem *item)
{
	if (filling_ || item->column() != value_column)
		return;
	const int row = item->row();
	const QString name = table_->item(row, name_column)->text();
	const bool as_bytes = item->data(Qt::UserRole).toBool();
	if (!session_.set(name, item->text(), as_bytes)) {
		say(tr("refused: %1").arg(session_.error()));
		populate();	// the cell goes back to what the bytes hold
		return;
	}
	populate();
	table_->setCurrentCell(row, value_column);
	say(session_.notes().isEmpty()
	    ? tr("%1 set").arg(name)
	    : session_.notes().join(QStringLiteral("; ")));
	setWindowModified(true);
}

void edit_window::open_dialog()
{
	const QString schema = QFileDialog::getOpenFileName(
	    this, tr("Open a schema"), QString(),
	    tr("situ schemas and images (*.situ *.img);;All files (*)"),
	    nullptr, dialog_options);
	if (schema.isEmpty())
		return;
	const QString message = QFileDialog::getOpenFileName(
	    this, tr("Open the bytes to read"), QString(), QString(),
	    nullptr, dialog_options);
	if (message.isEmpty())
		return;
	open_options options;
	options.schema  = schema;
	options.message = message;
	open(options, QString());
}

bool edit_window::save()
{
	if (out_.isEmpty())
		return save_as();
	if (!session_.save(out_)) {
		say(session_.error());
		return false;
	}
	say(tr("written to %1").arg(out_));
	return true;
}

bool edit_window::save_as()
{
	// Never the message opened, unless it is chosen here by name: as in
	// the CLI, nothing reaches a file nobody named.
	const QString path = QFileDialog::getSaveFileName(
	    this, tr("Write the edited bytes to"), QString(), QString(),
	    nullptr, dialog_options);
	if (path.isEmpty())
		return false;
	out_ = path;
	return save();
}

void edit_window::closeEvent(QCloseEvent *event)
{
	if (!session_.modified()) {
		event->accept();
		return;
	}
	const auto answer = QMessageBox::question(
	    this, tr("Unsaved edits"),
	    tr("The edited bytes have not been written. Write them first?"),
	    QMessageBox::Save | QMessageBox::Discard | QMessageBox::Cancel);
	if (answer == QMessageBox::Cancel ||
	    (answer == QMessageBox::Save && !save()))
		event->ignore();
	else
		event->accept();
}

QString edit_window::status_text() const
{
	return statusBar()->currentMessage();
}

QString edit_window::as_text() const
{
	QString text = header_->text().replace(QStringLiteral("<br>"),
	                                       QStringLiteral("\n"))
	               .remove(QStringLiteral("<b>"))
	               .remove(QStringLiteral("</b>")) + QLatin1Char('\n');
	for (int row = 0; row < table_->rowCount(); ++row) {
		QStringList cells;
		for (int column = 0; column < column_count; ++column)
			cells << table_->item(row, column)->text();
		text += cells.join(QLatin1Char('\t')).trimmed() + QLatin1Char('\n');
	}
	return text;
}

bool edit_window::write_out()
{
	return !out_.isEmpty() && session_.save(out_);
}

void edit_window::say(const QString &text)
{
	statusBar()->showMessage(text);
}
