"use strict";
(() => {
  function value(row) {
    const raw = row?.korean;
    let parsed = null;
    if (typeof raw === "boolean") parsed = raw;
    if (typeof raw === "string" || Number.isInteger(raw)) {
      const text = String(raw).trim().toLowerCase();
      if (["true", "1"].includes(text)) parsed = true;
      if (["false", "0"].includes(text)) parsed = false;
    }
    return parsed;
  }
  window.CollectorKorean = Object.freeze({
    value,
    matches: (row, selected) => selected === "all" || value(row) === ({true:true, false:false, unknown:null}[selected]),
    label: row => value(row) === true ? "한국" : value(row) === false ? "외국" : "-",
    restore: rows => rows.map(row => ({...row, korean:value(row)}))
  });
})();
