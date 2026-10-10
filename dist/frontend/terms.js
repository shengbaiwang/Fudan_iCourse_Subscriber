/* Keep these rules in sync with src/data/terms.py; raw catalog keys stay intact. */
window.ICS = window.ICS || {};
window.ICS.normalizeTerm = (value) => {
  const name = String(value || "").trim();
  const match = name.match(/^([0-9]{4})\s*[-–—]\s*([0-9]{4})\s*(?:学年\s*)?(?:[-–—]\s*)?(1|2|第[一二12]学期|暑期(?:学期)?)$/u);
  if (!match || Number(match[2]) !== Number(match[1]) + 1) return name;
  const labels = {"1": "第一学期", "第1学期": "第一学期", "第一学期": "第一学期",
    "2": "第二学期", "第2学期": "第二学期", "第二学期": "第二学期",
    "暑期": "暑期", "暑期学期": "暑期"};
  return `${match[1]}–${match[2]} ${labels[match[3]]}`;
};
window.ICS.termSortKey = (value) => {
  const name = window.ICS.normalizeTerm(value);
  const match = name.match(/^([0-9]{4})–([0-9]{4}) (第一学期|第二学期|暑期)$/u);
  if (!match) return name;
  const rank = {"第一学期": 1, "第二学期": 2, "暑期": 3}[match[3]];
  return `${match[1]}-${match[2]}-${rank}`;
};
window.ICS.compareTerms = (a, b) => window.ICS.termSortKey(a).localeCompare(window.ICS.termSortKey(b), "zh");
