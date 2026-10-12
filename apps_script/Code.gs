/**
 * leadforge <-> Google Sheets bridge (Apps Script web app).
 *
 * Paste into the spreadsheet's Extensions > Apps Script editor, then
 * Deploy > New deployment > Web app: "Execute as: Me", "Who has access: Anyone".
 * Put the web app URL in leadforge's .env as SHEETS_WEBAPP_URL.
 *
 * Every request is a POST with JSON {action, tab, ...}:
 *   ensure_tab  {tab, cols}                         create the tab if missing
 *   read        {tab}                               -> {values: [[...], ...]} as displayed text
 *   write_cells {tab, updates: [[row, col, [[...]]], ...]}   write blocks; never whole rows unless asked
 *   append      {tab, rows: [[...], ...]}           add rows at the end
 * Values are stored as plain text so Sheets does not reinterpret phones, IDs or dates.
 * Note: anyone who has the URL can call it. Keep the URL private.
 */

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var req = JSON.parse(e.postData.contents);
    var book = SpreadsheetApp.getActiveSpreadsheet();
    var out;
    switch (req.action) {
      case "ensure_tab": out = ensureTab(book, req.tab, req.cols || 26); break;
      case "read":       out = readTab(book, req.tab); break;
      case "write_cells": out = writeCells(sheetOrThrow(book, req.tab), req.updates || []); break;
      case "append":     out = appendRows(sheetOrThrow(book, req.tab), req.rows || []); break;
      default: throw new Error("unknown action: " + req.action);
    }
    out.status = "success";
    return json(out);
  } catch (err) {
    return json({status: "error", message: String(err)});
  } finally {
    lock.releaseLock();
  }
}

function doGet() {
  return json({status: "success", message: "leadforge bridge is running"});
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function sheetOrThrow(book, tab) {
  var sh = book.getSheetByName(tab);
  if (!sh) throw new Error("tab not found: " + tab);
  return sh;
}

function ensureTab(book, tab, cols) {
  var sh = book.getSheetByName(tab);
  var created = false;
  if (!sh) {
    sh = book.insertSheet(tab);
    created = true;
  }
  if (sh.getMaxColumns() < cols) sh.insertColumnsAfter(sh.getMaxColumns(), cols - sh.getMaxColumns());
  return {created: created};
}

function readTab(book, tab) {
  var sh = book.getSheetByName(tab);
  if (!sh || sh.getLastRow() === 0) return {values: []};
  var values = sh.getRange(1, 1, sh.getLastRow(), Math.max(sh.getLastColumn(), 1)).getDisplayValues();
  return {values: values};
}

function ensureSize(sh, rows, cols) {
  if (sh.getMaxRows() < rows) sh.insertRowsAfter(sh.getMaxRows(), rows - sh.getMaxRows());
  if (sh.getMaxColumns() < cols) sh.insertColumnsAfter(sh.getMaxColumns(), cols - sh.getMaxColumns());
}

function writeBlock(sh, row, col, block) {
  var width = 0;
  for (var i = 0; i < block.length; i++) width = Math.max(width, block[i].length);
  if (width === 0) return 0;
  var rect = block.map(function (r) {
    var line = r.map(function (v) { return v === null || v === undefined ? "" : String(v); });
    while (line.length < width) line.push("");
    return line;
  });
  ensureSize(sh, row + rect.length - 1, col + width - 1);
  var range = sh.getRange(row, col, rect.length, width);
  range.setNumberFormat("@");          // plain text: keep "+61 2 ...", "0414...", "2026-09-24" as typed
  range.setValues(rect);
  return rect.length * width;
}

function writeCells(sh, updates) {
  var cells = 0;
  for (var i = 0; i < updates.length; i++) {
    cells += writeBlock(sh, updates[i][0], updates[i][1], updates[i][2]);
  }
  return {cells: cells};
}

function appendRows(sh, rows) {
  if (!rows.length) return {rows: 0};
  var start = sh.getLastRow() + 1;
  writeBlock(sh, start, 1, rows);
  return {rows: rows.length, first_row: start};
}
