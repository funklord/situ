// window.cpp -- see window.h.

#include "window.h"

#include "cell_editor.h"

#ifndef SITU_VERSION
#define SITU_VERSION "unknown"
#endif

#include <QCloseEvent>
#include <QFileDialog>
#include <QFileInfo>
#include <QFontDatabase>
#include <QHeaderView>
#include <QInputDialog>
#include <QLineEdit>
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

}

edit_window::edit_window(QWidget *parent, document_hub *hub)
    : QMainWindow(parent)
    , hub_(hub ? hub : new document_hub(this))
{
	// A nested window is a window of its own, owned by the one it was
	// opened from so that it closes with it.
	if (parent != nullptr)
		setWindowFlag(Qt::Window);
	connect(hub_, &document_hub::changed, this, &edit_window::reload);
	auto *centre = new QWidget(this);
	auto *layout = new QVBoxLayout(centre);
	header_ = new QLabel(centre);
	header_->setTextInteractionFlags(Qt::TextSelectableByMouse);
	header_->setWordWrap(true);
	// The CLI's lines, verbatim: plain, so a `<` in a name is a `<`.
	header_->setTextFormat(Qt::PlainText);
	table_ = new QTableWidget(0, column_count, centre);
	table_->setHorizontalHeaderLabels({tr("Offset"), tr("Size"), tr("Field"),
	                                   tr("Value"), tr("Write"), tr("Note")});
	table_->verticalHeader()->hide();
	table_->setItemDelegateForColumn(value_column, new cell_editor(table_));
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

	// Edit: an element into or out of the run of structs on the current
	// row -- a PNG chunk -- each asked for and then the CLI's to make
	// (26.1147).
	QMenu *edit = menuBar()->addMenu(tr("&Edit"));
	edit->addAction(tr("&Insert Element..."), this,
	                &edit_window::insert_dialog);
	edit->addAction(tr("&Remove Element..."), this,
	                &edit_window::remove_dialog);

	// Help > About, where hydra, beerssh and fuzzypickles keep theirs: who
	// wrote it and which build this is.
	QMenu *help = menuBar()->addMenu(tr("&Help"));
	help->addAction(tr("&About situ-edit-gui"), this, [this] {
		QMessageBox::about(this, tr("About situ-edit-gui"), tr(
		    "<p><b>situ-edit-gui</b> %1</p>"
		    "<p>Reads and edits a message against a situ schema. Every "
		    "field, every write and every refusal is <tt>situ-edit</tt>'s; "
		    "this window draws them.</p><p>%2</p>")
		    .arg(QStringLiteral(SITU_VERSION),
		         QStringLiteral(SITU_COPYRIGHT).toHtmlEscaped()));
	});

	// A screen reader says what each part is: the table and the header
	// carry no visible caption of their own.
	table_->setAccessibleName(tr("Fields"));
	table_->setAccessibleDescription(tr(
	    "One row per field: offset, size, name, value, what a write costs, "
	    "and a note. Edit a value to write it; double-click a nested "
	    "struct to open it."));
	header_->setAccessibleName(tr("Document"));

	connect(table_, &QTableWidget::itemChanged, this, &edit_window::edited);
	// A double click on a nested struct opens it; on a writable value it
	// edits, which the table does itself. On a run of several structs it
	// asks which element, each named by where it is (26.1144).
	connect(table_, &QTableWidget::cellDoubleClicked, this,
	        [this](int row, int) {
		const QTableWidgetItem *name = table_->item(row, name_column);
		if (name == nullptr || name->data(Qt::UserRole).isNull())
			return;
		const QVariantList elements = name->data(Qt::UserRole).toMap()
		    .value(QStringLiteral("elements")).toList();
		if (elements.size() < 2) {
			open_field(name->text());
			return;
		}
		QStringList choices;
		for (qsizetype i = 0; i < elements.size(); ++i) {
			const QVariantMap one = elements[i].toMap();
			choices << tr("%1: at %2, %3 bytes")
			           .arg(i)
			           .arg(one.value(QStringLiteral("offset")).toLongLong())
			           .arg(one.value(QStringLiteral("length")).toLongLong());
		}
		bool chosen = false;
		const QString picked = QInputDialog::getItem(
		    this, tr("Open an element"),
		    tr("`%1` holds %2 elements; open which?")
		        .arg(name->text()).arg(elements.size()),
		    choices, 0, false, &chosen);
		if (chosen)
			open_field(name->text() + QLatin1Char(':')
			           + picked.section(QLatin1Char(':'), 0, 0));
	});
	setWindowTitle(QStringLiteral("situ-edit-gui"));
	resize(820, 560);
}

edit_window::~edit_window()
{
	// An editor still open when the window goes is closed by the table's
	// destruction, after `session_` is gone -- and closing commits, which
	// would write through a session that no longer exists. Measured: a
	// segfault in session::set from QWidget's destructor.
	filling_ = true;
}

bool edit_window::open(const open_options &options, const QString &out)
{
	out_ = out;
	if (!session_.open(options)) {
		say(session_.error());
		return false;
	}
	setWindowTitle(QStringLiteral("%1 -- %2 -- situ-edit-gui")
	               .arg(QFileInfo(options.message).fileName(),
	                    QFileInfo(options.schema).fileName()));
	populate();
	say(tr("Edit a value to set it; the schema decides what may be "
	       "written."));
	return true;
}

void edit_window::populate()
{
	// Every word below is the CLI's: `--format json`'s `view` is what the
	// text table and the curses TUI draw too (editor/view.py, 26.1113), so
	// the window derives no cell from a field and cannot word one
	// differently.
	const QJsonObject view = session_.document()
	                         .value(QStringLiteral("view")).toObject();
	QStringList heading;
	for (const QJsonValue &line : view.value(QStringLiteral("heading"))
	                              .toArray())
		heading << line.toString();
	for (const QJsonValue &line : view.value(QStringLiteral("trailer"))
	                              .toArray())
		heading << line.toString();
	header_->setText(heading.join(QLatin1Char('\n')));

	const QJsonArray rows = view.value(QStringLiteral("rows")).toArray();
	const QJsonArray fields = session_.document()
	                          .value(QStringLiteral("fields")).toArray();
	filling_ = true;
	table_->setRowCount(int(rows.size()));
	for (int at = 0; at < rows.size(); ++at) {
		const QJsonObject row = rows[at].toObject();
		auto cell = [&row](const char *key) {
			auto *item = new QTableWidgetItem(
			    row.value(QLatin1String(key)).toString());
			item->setFlags(Qt::ItemIsSelectable | Qt::ItemIsEnabled);
			return item;
		};
		// Offsets, sizes and values in the desktop's fixed font: hex
		// lines up digit under digit, and a 0 is not an O.
		const QFont fixed = QFontDatabase::systemFont(QFontDatabase::FixedFont);
		QTableWidgetItem *offset = cell("offset");
		offset->setFont(fixed);
		table_->setItem(at, offset_column, offset);
		QTableWidgetItem *size = cell("size");
		size->setFont(fixed);
		table_->setItem(at, size_column, size);
		QTableWidgetItem *named = cell("name");
		// The nested struct a double click opens, as the JSON gives it.
		const QJsonValue nested = fields[at].toObject()
		                          .value(QStringLiteral("nested"));
		if (nested.isObject()) {
			// With the run's elements beside it, where the field is a run
			// of structs: what `name:N` and the double click choose from.
			QVariantMap opens = nested.toObject().toVariantMap();
			opens[QStringLiteral("elements")] = fields[at].toObject()
			    .value(QStringLiteral("elements")).toVariant();
			named->setData(Qt::UserRole, opens);
			named->setToolTip(tr("Double-click to open the nested `%1`")
			                  .arg(nested.toObject()
			                       .value(QStringLiteral("struct"))
			                       .toString()));
		}
		table_->setItem(at, name_column, named);

		QTableWidgetItem *value = cell("value");
		value->setFont(fixed);
		if (!row.value(QStringLiteral("edit")).isNull())
			value->setFlags(value->flags() | Qt::ItemIsEditable);
		// The row the editor is chosen and constrained by (cell_editor).
		value->setData(view_row_role, row.toVariantMap());
		table_->setItem(at, value_column, value);

		// What a write would do: the marker the table prints, and the
		// sentence behind it -- the part no template-driven hex editor
		// can show (0034).
		QTableWidgetItem *write = cell("marker");
		write->setToolTip(row.value(QStringLiteral("cost")).toString());
		table_->setItem(at, write_column, write);
		table_->setItem(at, note_column, cell("note"));
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
	if (!session_.set(name, item->text())) {
		say(tr("refused: %1").arg(session_.error()));
		populate();	// the cell goes back to what the bytes hold
		return;
	}
	const QString said = session_.notes().isEmpty()
	                     ? tr("%1 set").arg(name)
	                     : session_.notes().join(QStringLiteral("; "));
	// Every window over these bytes re-reads them, this one included.
	emit hub_->changed();
	table_->setCurrentCell(row, value_column);
	say(said);
}

// The run of structs on `row`, and how many elements it holds: false where
// the row is not one.
bool edit_window::elements_of(int row, QString *field, int *count) const
{
	const QTableWidgetItem *name = table_->item(row, name_column);
	if (name == nullptr || name->data(Qt::UserRole).isNull())
		return false;
	const QVariant elements = name->data(Qt::UserRole).toMap()
	    .value(QStringLiteral("elements"));
	if (elements.isNull())
		return false;
	*field = name->text();
	*count = int(elements.toList().size());
	return true;
}

void edit_window::insert_dialog()
{
	QString field;
	int count = 0;
	if (!elements_of(table_->currentRow(), &field, &count)) {
		say(tr("select a run of structs to insert an element into"));
		return;
	}
	bool chosen = false;
	const int which = QInputDialog::getInt(
	    this, tr("Insert an element"),
	    tr("`%1` holds %2 elements; the new one goes in at:")
	        .arg(field).arg(count),
	    count, 0, count, 1, &chosen);
	if (!chosen)
		return;
	const QString hex = QInputDialog::getText(
	    this, tr("Insert an element"),
	    tr("Its bytes, in hex -- a checksum situ computes is computed:"),
	    QLineEdit::Normal, QString(), &chosen);
	if (chosen)
		insert_element(field, which, hex);
}

void edit_window::remove_dialog()
{
	QString field;
	int count = 0;
	if (!elements_of(table_->currentRow(), &field, &count) || count == 0) {
		say(tr("select a run of structs with an element to remove"));
		return;
	}
	bool chosen = false;
	const int which = QInputDialog::getInt(
	    this, tr("Remove an element"),
	    tr("`%1` holds %2 elements; remove number:").arg(field).arg(count),
	    0, 0, count - 1, 1, &chosen);
	if (chosen)
		remove_element(field, which);
}

bool edit_window::insert_element(const QString &field, int which,
                                 const QString &hex)
{
	const bool done = session_.insert(field, which, hex);
	spliced(field, done);
	return done;
}

bool edit_window::remove_element(const QString &field, int which)
{
	const bool done = session_.remove(field, which);
	spliced(field, done);
	return done;
}

void edit_window::spliced(const QString &field, bool done)
{
	if (!done) {
		say(tr("refused: %1").arg(session_.error()));
		return;
	}
	say(session_.notes().isEmpty()
	    ? tr("%1 changed").arg(field)
	    : session_.notes().join(QStringLiteral("; ")));
	// Every window over these bytes re-reads them, this one included.
	emit hub_->changed();
}

void edit_window::reload()
{
	if (!session_.refresh()) {
		say(session_.error());
		return;
	}
	populate();
}

edit_window *edit_window::open_field(const QString &which)
{
	// `name:N` is element N of a run of structs, where the JSON lists the
	// elements: opening the run whole reads its first, and a PNG's `tEXt`
	// after IHDR was reachable from no frontend (26.1144).
	QString field = which;
	int element = -1;
	const qsizetype colon = which.lastIndexOf(QLatin1Char(':'));
	if (colon > 0) {
		bool number = false;
		const int n = which.mid(colon + 1).toInt(&number);
		if (number && n >= 0) {
			field = which.left(colon);
			element = n;
		}
	}
	for (int row = 0; row < table_->rowCount(); ++row) {
		const QTableWidgetItem *name = table_->item(row, name_column);
		if (name->text() != field)
			continue;
		const QVariant nested = name->data(Qt::UserRole);
		if (nested.isNull()) {
			say(tr("`%1` is not a nested struct").arg(field));
			return nullptr;
		}
		QVariantMap opened = nested.toMap();
		if (element >= 0) {
			const QVariantList elements
			    = opened.value(QStringLiteral("elements")).toList();
			if (element >= elements.size()) {
				say(tr("`%1` has %n element(s)", nullptr,
				       int(elements.size())).arg(field));
				return nullptr;
			}
			const QVariantMap one = elements[element].toMap();
			opened[QStringLiteral("offset")] = one.value(QStringLiteral("offset"));
			opened[QStringLiteral("length")] = one.value(QStringLiteral("length"));
		}
		auto *child = new edit_window(this, hub_);
		child->setAttribute(Qt::WA_DeleteOnClose);
		if (!child->open_nested(session_,
		                        QJsonObject::fromVariantMap(opened),
		                        out_)) {
			say(tr("cannot open `%1`: %2").arg(field, child->error()));
			delete child;
			return nullptr;
		}
		child->show();
		return child;
	}
	say(tr("no field `%1`").arg(field));
	return nullptr;
}

bool edit_window::open_nested(const session &parent, const QJsonObject &nested,
                              const QString &out)
{
	out_ = out;
	const QString   type   = nested.value(QStringLiteral("struct")).toString();
	const qint64    offset = nested.value(QStringLiteral("offset")).toInteger();
	const QJsonValue length = nested.value(QStringLiteral("length"));
	if (!session_.open_nested(parent, type, offset,
	                          length.isNull() ? -1 : length.toInteger()))
		return false;
	setWindowTitle(QStringLiteral("%1 at %2 -- situ-edit-gui")
	               .arg(type).arg(offset));
	populate();
	say(tr("A nested struct, over the same bytes as the window it was "
	       "opened from."));
	return true;
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
	// A nested window's edits are its document's, and the document's first
	// window is the one that asks about them.
	if (parentWidget() != nullptr || !session_.modified()) {
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
	QString text = header_->text() + QLatin1Char('\n');
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
