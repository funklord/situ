// cell_editor.cpp -- see cell_editor.h.

#include "cell_editor.h"

#include <QAbstractItemView>
#include <QComboBox>
#include <QCompleter>
#include <QLineEdit>
#include <QRegularExpression>
#include <QRegularExpressionValidator>
#include <QValidator>

namespace {

// A closed enum's text: a member as the row shows it (`server (4)`) or by
// name is acceptable; anything some member contains is on its way to one;
// nothing else can be typed. An open enum also takes a number.
class member_validator : public QValidator {
public:
	member_validator(const QStringList &choices, bool admits_other,
	                 QObject *parent)
	    : QValidator(parent), choices_(choices), admits_other_(admits_other)
	{
	}

	State validate(QString &input, int &) const override
	{
		const QString typed = input.trimmed();
		if (typed.isEmpty())
			return Intermediate;
		bool partial = false;
		for (const QString &choice : choices_) {
			const QString name = choice.section(QStringLiteral(" ("), 0, 0);
			if (typed == choice || typed == name)
				return Acceptable;
			if (choice.contains(typed, Qt::CaseInsensitive))
				partial = true;
		}
		if (admits_other_ && number().match(typed).hasMatch())
			return Acceptable;
		if (admits_other_ && (typed == QStringLiteral("-") ||
		                      typed.compare(QStringLiteral("0x"),
		                                    Qt::CaseInsensitive) == 0))
			return Intermediate;
		return partial ? Intermediate : Invalid;
	}

	static const QRegularExpression &number()
	{
		static const QRegularExpression pattern(QStringLiteral(
		    "^-?(0[xX][0-9a-fA-F]+|[0-9]+)$"));
		return pattern;
	}

private:
	QStringList choices_;
	bool        admits_other_;
};

// A number within the field's range: decimal, or hex after `0x`, with a
// sign only where the range goes below zero. `low` and `high` come from the
// view as text, because a 64-bit bound is not a double. Typing can only make
// a number's magnitude grow, so a value already past a bound is refused at
// the key; one short of a positive minimum is on its way there.
class number_validator : public QValidator {
public:
	number_validator(const QString &low, const QString &high, QObject *parent)
	    : QValidator(parent)
	    , bounded_(!low.isEmpty() && !high.isEmpty())
	    , low_negative_(low.startsWith(QLatin1Char('-')))
	    , low_(magnitude(low))
	    , high_negative_(high.startsWith(QLatin1Char('-')))
	    , high_(magnitude(high))
	{
	}

	State validate(QString &input, int &) const override
	{
		static const QRegularExpression shape(QStringLiteral(
		    "^(-?)(0[xX]([0-9a-fA-F]*)|([0-9]*))$"));
		const QRegularExpressionMatch match = shape.match(input.trimmed());
		if (!match.hasMatch())
			return Invalid;
		const bool negative = !match.captured(1).isEmpty();
		if (negative && bounded_ && !low_negative_)
			return Invalid;	// no sign on a field that cannot hold one
		const QString hex = match.captured(3);
		const QString decimal = match.captured(4);
		if (hex.isEmpty() && decimal.isEmpty())
			return Intermediate;	// "", "-", "0x"
		bool ok = false;
		const quint64 value = hex.isEmpty() ? decimal.toULongLong(&ok)
		                                    : hex.toULongLong(&ok, 16);
		if (!ok)
			return Invalid;		// past 64 bits: past every bound
		if (!bounded_)
			return Acceptable;
		if (negative) {
			if (value > low_)
				return Invalid;
			return (high_negative_ && value < high_) ? Intermediate
			                                         : Acceptable;
		}
		if (high_negative_ || value > high_)
			return Invalid;
		if (!low_negative_ && value < low_)
			return Intermediate;
		return Acceptable;
	}

private:
	static quint64 magnitude(const QString &bound)
	{
		QString digits = bound;
		if (digits.startsWith(QLatin1Char('-')))
			digits.remove(0, 1);
		return digits.toULongLong();
	}

	bool    bounded_;
	bool    low_negative_;
	quint64 low_;
	bool    high_negative_;
	quint64 high_;
};

QLineEdit *line_of(QWidget *editor)
{
	if (auto *box = qobject_cast<QComboBox *>(editor))
		return box->lineEdit();
	return qobject_cast<QLineEdit *>(editor);
}

}

QWidget *cell_editor::createEditor(QWidget *parent,
                                   const QStyleOptionViewItem &option,
                                   const QModelIndex &index) const
{
	const QVariantMap row = index.data(view_row_role).toMap();
	const QString edit = row.value(QStringLiteral("edit")).toString();

	if (edit == QStringLiteral("choice")) {
		const QStringList choices =
		    row.value(QStringLiteral("choices")).toStringList();
		auto *box = new QComboBox(parent);
		box->setEditable(true);
		box->addItems(choices);
		// The list is the schema's, so typing never adds to it.
		box->setInsertPolicy(QComboBox::NoInsert);
		auto *finder = new QCompleter(choices, box);
		finder->setFilterMode(Qt::MatchContains);
		finder->setCaseSensitivity(Qt::CaseInsensitive);
		finder->setCompletionMode(QCompleter::PopupCompletion);
		box->setCompleter(finder);
		box->setValidator(new member_validator(
		    choices, row.value(QStringLiteral("admits_other")).toBool(),
		    box));
		// RETURN DID NOTHING. The view's delegate commits on Return through
		// an event filter on the editor, and an editable combo's line edit
		// and completer take the key first, so the choice sat in an open
		// editor until focus happened to leave it -- measured, through
		// Qt's delivery and qtty's alike. So commit and close on the line
		// edit's own Return, which Qt emits only for ACCEPTABLE text, and
		// on a pick from the completer's list.
		auto *self = const_cast<cell_editor *>(this);
		auto done = [self, box] {
			emit self->commitData(box);
			emit self->closeEditor(box, QAbstractItemDelegate::NoHint);
		};
		connect(box->lineEdit(), &QLineEdit::returnPressed, box, done);
		connect(finder, qOverload<const QString &>(&QCompleter::activated),
		        box, [box, done](const QString &picked) {
			box->setEditText(picked);
			done();
		});
		return box;
	}

	auto *line = qobject_cast<QLineEdit *>(
	    QStyledItemDelegate::createEditor(parent, option, index));
	if (line == nullptr)
		return QStyledItemDelegate::createEditor(parent, option, index);
	if (edit == QStringLiteral("hex")) {
		line->setValidator(new QRegularExpressionValidator(
		    QRegularExpression(QStringLiteral("[0-9a-fA-F]*")), line));
		// A run is written at the length it has, so no more digits than
		// it holds can be typed -- unless the schema lets it change length
		// and the core moves what sizes it (26.1115).
		const int size = row.value(QStringLiteral("size")).toString().toInt();
		if (size > 0 && !row.value(QStringLiteral("resizable")).toBool())
			line->setMaxLength(2 * size);
	} else if (edit == QStringLiteral("number")) {
		line->setValidator(new number_validator(
		    row.value(QStringLiteral("low")).toString(),
		    row.value(QStringLiteral("high")).toString(), line));
	}
	return line;
}

void cell_editor::setEditorData(QWidget *editor, const QModelIndex &index) const
{
	const QString shown = index.data(Qt::EditRole).toString();
	if (auto *box = qobject_cast<QComboBox *>(editor)) {
		const int at = box->findText(shown);
		if (at >= 0)
			box->setCurrentIndex(at);
		else
			box->setEditText(shown);
	} else {
		QStyledItemDelegate::setEditorData(editor, index);
	}
	// Typing replaces the value, as in a spreadsheet: a person editing a
	// cell is giving it a new value, not appending to the old one.
	if (QLineEdit *line = line_of(editor))
		line->selectAll();
}

void cell_editor::setModelData(QWidget *editor, QAbstractItemModel *model,
                               const QModelIndex &index) const
{
	QLineEdit *line = line_of(editor);
	if (line == nullptr)
		return QStyledItemDelegate::setModelData(editor, model, index);
	// Not accepted, not committed: Qt closes an editor on Return and on
	// focus leaving and commits what it holds either way, validator or
	// not, and an Intermediate `serv` or `0x` would reach the core.
	if (!line->hasAcceptableInput())
		return;
	const QString typed = line->text().trimmed();
	if (typed == index.data(Qt::EditRole).toString())
		return;			// nothing changed: no write, no dirty flag
	model->setData(index, typed, Qt::EditRole);
}
