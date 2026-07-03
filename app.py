var CRM_STOCK_URL = 'https://spinny-3dp-crm.onrender.com/api/stock_update';
var CRM_RESET_URL = 'https://spinny-3dp-crm.onrender.com/api/stock_sheet_reset';

function getStockRows() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName('Log');
  var data = sheet.getDataRange().getValues();
  var rows = [];
  for (var i = 2; i < data.length; i++) {
    var r = data[i];
    var dateVal = r[0], timeVal = r[1], city = r[2], material = r[3], inout = r[4], qty = r[5], enteredBy = r[6];
    if (!dateVal || !city || !material || !inout) continue;

    var dateStr;
    if (dateVal instanceof Date) {
      dateStr = Utilities.formatDate(dateVal, 'Asia/Kolkata', 'yyyy-MM-dd');
    } else {
      var s = String(dateVal).trim();
      var m = s.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})$/);
      if (m) {
        var dd = ('0' + m[1]).slice(-2);
        var mm = ('0' + m[2]).slice(-2);
        dateStr = m[3] + '-' + mm + '-' + dd;
      } else {
        dateStr = s;
      }
    }

    var timeStr;
    if (timeVal instanceof Date) {
      timeStr = Utilities.formatDate(timeVal, 'Asia/Kolkata', 'HH:mm');
    } else {
      timeStr = String(timeVal || '').trim();
    }

    var rawKey = dateStr + '|' + timeVal + '|' + city + '|' + material + '|' + inout + '|' + qty + '|' + enteredBy + '|' + i;
    var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, rawKey);
    var sourceId = Utilities.base64EncodeWebSafe(digest);

    rows.push({
      source_id: sourceId,
      date: dateStr,
      time: timeStr,
      city: String(city).trim(),
      material: String(material).trim(),
      direction: String(inout).trim().toUpperCase(),
      qty: qty,
      entered_by: String(enteredBy || '').trim()
    });
  }
  return rows;
}

function pushStockToCRM() {
  var payload = JSON.stringify({ rows: getStockRows() });
  var res = UrlFetchApp.fetch(CRM_STOCK_URL, {
    method: 'post',
    contentType: 'application/json',
    payload: payload,
    muteHttpExceptions: true
  });
  Logger.log('Stock pushed to CRM: ' + res.getContentText());
}

function resetStockInCRM() {
  var res = UrlFetchApp.fetch(CRM_RESET_URL, {
    method: 'post',
    muteHttpExceptions: true
  });
  Logger.log('Reset result: ' + res.getContentText());
}

function setupStockPushTrigger() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'pushStockToCRM') {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  ScriptApp.newTrigger('pushStockToCRM').timeBased().everyMinutes(30).create();
  pushStockToCRM();
  SpreadsheetApp.getActiveSpreadsheet().toast('Stock sent + auto-sync every 30 min!', 'Spinny 3DP', 5);
}
