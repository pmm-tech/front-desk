// Copyright (c) 2021, Core Initiative and contributors
// For license information, please see license.txt

var doc = undefined;

frappe.ui.form.on('Inn Room Type Availability Page', {
	search_button: function(frm, cdt, cdn) {
		doc = locals[cdt][cdn];
		search(doc);
	},
	onload: function (frm) {
		frm.disable_save();
	},
	start: function(frm) {
		if (frm.doc.end != null && (frm.doc.end < frm.doc.start)) {
			frm.doc.start = null;
			frappe.msgprint(__("End date must be greater than Start date"));
			frm.refresh();
		}
	},
	end: function (frm) {
		if (frm.doc.start != null && (frm.doc.end < frm.doc.start)) {
			frm.doc.end = null;
			frappe.msgprint(__("End date must be greater than Start date"));
			frm.refresh();
		}
	}
});

function search(doc) {
	if (doc.start == undefined || doc.end == undefined) {
		return;
	}

	var wrapper = cur_frm.get_field('html').$wrapper;
	var html =	'<div id="room-calendar">\
					<table class="form-grid" id="table-calendar">\
						<tr class="grid-heading-row" id="table-calendar-title">\
							<th class="frozen frozen-room-type">Room Type</th>\
							<th class="frozen frozen-bed-type">Bed Type</th>\
						</tr>\
					</table>\
				</div>';
	var css = 	'<style>\
					#room-calendar {\
						font-size:12px;\
						height:100%;\
						max-height:600px;\
						overflow-y:scroll;\
					}\
					.frozen{\
						height: 55px;\
						padding: 10px 15px;\
						border: 1px solid #d1d8dd;\
						text-align: center; \
						font-weight: bold;\
						background-color:#f7fafc;\
					}\
					.frozen-room-type{\
						width: 120px;\
					}\
					.frozen-bed-type{\
						width: 90px;\
					}\
					.room-type-cell{\
						vertical-align: middle;\
						text-align: left;\
					}\
					td.grid-static-col.overbooked{\
						color: #b00020;\
						font-weight: 600;\
					}\
					.empty-row{\
						text-align: center;\
						color: #6c7680;\
					}\
				</style>';
	wrapper.html(html+css);

	var start = parseIsoDate(doc.start);
	var end = parseIsoDate(doc.end);
	var dates = [];
	for (var day = start; day <= end; day = addDays(day, 1)) {
		dates.push(formatDate(day));
	}

	var title = document.getElementById('table-calendar-title');
	dates.forEach(function(date) {
		var th = document.createElement('th');
		th.className = 'grid-static-col';
		th.innerHTML = date.split("-").reverse().join("-");
		title.appendChild(th);
	});

	var table = document.getElementById('table-calendar');
	var loading = buildMessageRow(dates, __('Loading...'));
	table.appendChild(loading);

	frappe.call({
		method: 'inn.inn_hotels.doctype.inn_room_type_availability_page.inn_room_type_availability_page.get_room_type_bed_type_availability',
		args: {
			start: doc.start,
			end: doc.end
		},
		freeze: true,
		callback: function(resp) {
			loading.remove();
			var data = resp.message || {};
			var rows = data.rows || [];
			if (!rows.length) {
				table.appendChild(buildMessageRow(dates, __('No rooms configured')));
				return;
			}
			renderRows(table, rows, dates);
		},
		error: function() {
			loading.remove();
		}
	});
}

// One row per room type and bed type. The room type is printed once per group
// by spanning the group's rows, so a type with two bed types reads as one block.
function renderRows(table, rows, dates) {
	var index = 0;
	while (index < rows.length) {
		var room_type = rows[index].room_type;
		var group_end = index;
		while (group_end < rows.length && rows[group_end].room_type === room_type) {
			group_end++;
		}

		for (var position = index; position < group_end; position++) {
			var row_span = position === index ? group_end - index : 0;
			table.appendChild(buildRow(rows[position], dates, row_span));
		}

		index = group_end;
	}
}

// row_span = 0 marks a continuation row: the Room Type cell of the group's
// first row already spans this one, so this row must NOT emit a cell for that
// column (doing so would push every following cell one column to the right).
function buildRow(row, dates, row_span) {
	var tr = document.createElement('tr');
	tr.className = 'grid-row';

	if (row_span > 0) {
		var room_type = document.createElement('td');
		room_type.className = 'frozen room-type-cell';
		room_type.innerHTML = frappe.utils.escape_html(row.room_type || '');
		if (row_span > 1) {
			room_type.rowSpan = row_span;
		}
		tr.appendChild(room_type);
	}

	var bed_type = document.createElement('td');
	bed_type.className = 'frozen bed-type-cell';
	if (row.bed_type) {
		bed_type.innerHTML = frappe.utils.escape_html(row.bed_type);
	} else {
		bed_type.innerHTML = '&mdash;';
		bed_type.title = __('No bed type set');
	}
	tr.appendChild(bed_type);

	dates.forEach(function(date) {
		var available = (row.available || {})[date];
		var td = document.createElement('td');
		td.className = 'grid-static-col';
		if (available === undefined || available === null) {
			td.innerHTML = '&mdash;';
		} else {
			td.innerHTML = available + ' / ' + row.total;
			td.title = __('{0} available of {1}', [available, row.total]);
			if (available < 0) {
				td.className += ' overbooked';
			}
		}
		tr.appendChild(td);
	});

	return tr;
}

function buildMessageRow(dates, message) {
	var tr = document.createElement('tr');
	tr.className = 'grid-row';

	var td = document.createElement('td');
	td.className = 'grid-static-col empty-row';
	td.colSpan = 2 + dates.length;
	td.innerHTML = frappe.utils.escape_html(message);
	tr.appendChild(td);

	return tr;
}

// Build the date in local time: `new Date('2026-03-24')` parses as UTC and can
// render the previous day west of Greenwich.
function parseIsoDate(value) {
	var parts = String(value).split('-');
	return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
}

function addDays(date, days) {
	var next = new Date(date.getFullYear(), date.getMonth(), date.getDate());
	next.setDate(next.getDate() + days);
	return next;
}

function formatDate(date) {
    var d = new Date(date),
        month = '' + (d.getMonth() + 1),
        day = '' + d.getDate(),
        year = d.getFullYear();

    if (month.length < 2)
        month = '0' + month;
    if (day.length < 2)
        day = '0' + day;

    return [year, month, day].join('-');
}
