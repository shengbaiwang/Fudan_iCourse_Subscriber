/* Keep these conservative rules in sync with src/data/departments.py. */
window.ICS = window.ICS || {};
window.ICS.normalizeDepartment = (value) => String(value || "")
  .replace(/[\t\n\v\f\r \u00a0\u3000]+/g, " ").trim()
  .replace(/^(?:[0-9０-９]{3} )+(?=\S)/u, "");
