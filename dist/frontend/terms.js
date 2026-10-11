/* Keep normalization and sorting in sync with src/data/terms.py; raw catalog keys stay intact. */
window.ICS = window.ICS || {};
window.ICS.normalizeTerm = (value) => {
  const name = String(value || "").trim();
  const match = name.match(/^([0-9]{4})\s*[-–—]\s*([0-9]{4})\s*(?:学年\s*)?(?:[-–—]\s*)?(1|2|第[一二12]学期|[秋春]季(?:学期)?|暑期(?:学期)?)$/u);
  if (!match || Number(match[2]) !== Number(match[1]) + 1) return name;
  const labels = {"1": "秋季", "第1学期": "秋季", "第一学期": "秋季",
    "2": "春季", "第2学期": "春季", "第二学期": "春季",
    "秋季": "秋季", "秋季学期": "秋季", "春季": "春季", "春季学期": "春季",
    "暑期": "暑期", "暑期学期": "暑期"};
  return `${match[1]}–${match[2]} ${labels[match[3]]}`;
};
// Display the season's calendar year without changing stored terms or filter values.
window.ICS.formatTerm = (value) => {
  const name = window.ICS.normalizeTerm(value);
  const academic = name.match(/^([0-9]{4})–([0-9]{4}) (秋季|春季|暑期)$/u);
  if (academic) {
    const year = academic[3] === "秋季" ? academic[1] : academic[2];
    return `${year} ${academic[3][0]}`;
  }
  const calendar = name.match(/^([0-9]{4})\s*(?:[-–—]\s*)?(秋(?:季(?:学期)?)?|春(?:季(?:学期)?)?|暑(?:期(?:学期)?)?)$/u);
  return calendar ? `${calendar[1]} ${calendar[2][0]}` : name;
};
window.ICS.termSortKey = (value) => {
  const name = window.ICS.normalizeTerm(value);
  const match = name.match(/^([0-9]{4})–([0-9]{4}) (秋季|春季|暑期)$/u);
  if (!match) return name;
  const rank = {"秋季": 1, "春季": 2, "暑期": 3}[match[3]];
  return `${match[1]}-${match[2]}-${rank}`;
};
window.ICS.compareTerms = (a, b) => window.ICS.termSortKey(a).localeCompare(window.ICS.termSortKey(b), "zh");
// Conservative fallback cutoffs; unknown terms keep checking.
window.ICS.termEndDate = (value) => {
  const name = window.ICS.normalizeTerm(value);
  const academic = name.match(/^([0-9]{4})–([0-9]{4}) (秋季|春季|暑期)$/u);
  const calendar = name.match(/^([0-9]{4})\s*[-–—]?\s*([秋春暑])(?:季|期)?(?:学期)?$/u);
  if (!academic && !calendar) return null;
  const season = academic ? academic[3][0] : calendar[2];
  const year = academic ? Number(academic[2]) : Number(calendar[1]) + (season === "秋" ? 1 : 0);
  if (year < 1 || year > 9999) return null;
  return `${String(year).padStart(4, "0")}-${{秋:"03", 春:"08", 暑:"09"}[season]}-01`;
};
window.ICS.courseToday = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
