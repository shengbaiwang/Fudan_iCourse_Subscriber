/**
 * sql.js wrapper — load lecture data from sharded encrypted shards.
 *
 * In the sharded layout (current), each shard is a self-contained sqlite
 * file holding only the courses + lectures + summary_versions + ppt_pages
 * rows for the courses
 * it owns. The page reassembles them in-memory by copying every shard's
 * rows into a single working SQL.Database. This avoids ATTACH'ing across
 * sql.js DB instances — sql.js doesn't support cross-file ATTACH cleanly,
 * and the row-count is small enough that copying is instantaneous.
 */

window.ICS = window.ICS || {};

// Semester terms that predate the recording system — never shown anywhere.
const _INVALID_TERMS_GLOB = ["*_19_*"];
const _INVALID_TERMS_EXACT = ["25"];

const _SQL_CDN = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.12.0";
let _db = null;
let _SQL = null;

function _schemaSql() {
  // schema.js loads before this file via index.html and registers the SQL
  // on window.ICS.schema so backend (src/data/schema.py) and frontend share one
  // source of truth.  Throw early if it's missing — silent NULL would
  // produce "no such table" later, which is a worse failure mode.
  var s = window.ICS && window.ICS.schema && window.ICS.schema.SCHEMA_SQL;
  if (!s) throw new Error("ICS.schema.SCHEMA_SQL missing — load browser/schema.js first");
  return s;
}

async function _ensureSqlJs() {
  if (_SQL) return _SQL;
  _SQL = await window.initSqlJs({
    locateFile: (file) => `${_SQL_CDN}/${file}`,
  });
  return _SQL;
}

async function _initFromBytes(dbBytes) {
  // Legacy path — accepts a single monolithic sqlite file.
  // Kept so older deployments (pre-shard data branch) still load.
  const SQL = await _ensureSqlJs();
  if (_db) _db.close();
  _db = dbBytes ? new SQL.Database(dbBytes) : new SQL.Database();
  if (!dbBytes) _db.exec(_schemaSql());
  // Ensure new tables exist when loading a cached DB from an older version
  _db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
  _db.exec(
    "CREATE TABLE IF NOT EXISTS summary_versions (" +
    "sub_id TEXT NOT NULL, model TEXT NOT NULL, summary TEXT NOT NULL, " +
    "generated_at TEXT NOT NULL, PRIMARY KEY (sub_id, model, generated_at))"
  );
}

async function _initEmpty() {
  const SQL = await _ensureSqlJs();
  if (_db) _db.close();
  _db = new SQL.Database();
  _db.exec(_schemaSql());
  _db.exec("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)");
}

function _exportDB() {
  // Returns the full merged DB as Uint8Array (for caching in IndexedDB).
  if (!_db) throw new Error("Database not initialized");
  return _db.export();
}

function _copyRows(src, dst, table) {
  const result = src.exec(`SELECT * FROM ${table}`);
  if (!result.length || !result[0].values.length) return;
  // Shard may have extra columns that the frontend's schema no longer
  // defines (e.g. summary_format_version).  Only copy columns that
  // actually exist in the destination table.
  var dstCols = {};
  try {
    var info = dst.exec("PRAGMA table_info(" + table + ")");
    if (info.length && info[0].values) {
      for (var i = 0; i < info[0].values.length; i++) {
        dstCols[info[0].values[i][1]] = true;
      }
    }
  } catch (e) { /* fall through — use all source columns */ }
  const srcCols = result[0].columns;
  var cols = srcCols;
  var colIndexes = null;
  if (Object.keys(dstCols).length) {
    cols = [];
    colIndexes = [];
    for (var j = 0; j < srcCols.length; j++) {
      if (dstCols[srcCols[j]]) {
        cols.push(srcCols[j]);
        colIndexes.push(j);
      }
    }
  }
  if (!cols.length) return;
  const placeholders = cols.map(function () { return "?"; }).join(",");
  const stmt = dst.prepare(
    "INSERT OR IGNORE INTO " + table + " (" + cols.join(",") + ") VALUES (" + placeholders + ")"
  );
  for (var k = 0; k < result[0].values.length; k++) {
    var row = result[0].values[k];
    if (colIndexes) {
      var filtered = [];
      for (var m = 0; m < colIndexes.length; m++) {
        filtered.push(row[colIndexes[m]]);
      }
      stmt.bind(filtered);
    } else {
      stmt.bind(row);
    }
    stmt.step();
    stmt.reset();
  }
  stmt.free();
}

async function _attachShard(shardBytes) {
  if (!_db) throw new Error("Database not initialized");
  const SQL = await _ensureSqlJs();
  const shard = new SQL.Database(shardBytes);
  // Check which tables exist in the shard — the ``meta`` table is new
  // (added 2026-05) and old shards don't have it.
  var shardTables = {};
  try {
    var tableRows = shard.exec("SELECT name FROM sqlite_master WHERE type='table'");
    if (tableRows.length && tableRows[0].values) {
      for (var i = 0; i < tableRows[0].values.length; i++) {
        shardTables[tableRows[0].values[i][0]] = true;
      }
    }
  } catch (e) { /* old sql.js version — fall through */ }
  try {
    _copyRows(shard, _db, "courses");
    _copyRows(shard, _db, "lectures");
    if (shardTables["summary_versions"]) _copyRows(shard, _db, "summary_versions");
    _copyRows(shard, _db, "ppt_pages");
    _copyRows(shard, _db, "all_courses");
    if (shardTables["meta"]) _copyRows(shard, _db, "meta");
  } finally {
    shard.close();
  }
}

function _deriveState(row) {
  // "no_video" is a soft error stage the backend records when the lecture
  // has no playable video yet (it retries a few runs in case the recording
  // appears later).  Render it as a gray informational badge, not a red
  // failure.
  if (row.error_stage === "no_video") return "novideo";
  if (row.error_stage) return "failed";
  if (row.summary && row.processed_at) return "ready";
  // Processed, no summary, no error = a lecture the backend permanently
  // skipped (no audio stream / empty transcript) and marked done.
  // Distinct from "waiting" (enqueued, not yet run) so these don't show
  // as perpetually pending in the UI.
  if (row.processed_at) return "skipped";
  if (row.transcript) return "processing";
  return "waiting";
}

function _queryAll(sql, params) {
  if (!_db) return [];
  const stmt = _db.prepare(sql);
  if (params) stmt.bind(params);
  const results = [];
  while (stmt.step()) results.push(stmt.getAsObject());
  stmt.free();
  return results;
}

function _getCourses() {
  return _queryAll(`
    SELECT c.course_id AS course_id, c.title AS title, c.teacher AS teacher,
           COUNT(CASE WHEN l.summary IS NOT NULL THEN 1 END) AS summary_count,
           COUNT(l.sub_id) AS total_count,
           MAX(l.processed_at) AS last_updated
    FROM courses c
    LEFT JOIN lectures l ON c.course_id = l.course_id
    GROUP BY c.course_id
    ORDER BY last_updated DESC NULLS LAST
  `);
}

// Sort key parsed straight from sub_title — we deliberately do NOT trust the
// `date` column. `date` is itself just substr(sub_title,1,10) on the backend
// and is inconsistently back-filled: unprocessed lectures often ship with an
// empty `date`, and `ORDER BY date` then floats that whole block ahead of the
// populated rows (the cause of the two-run, out-of-chronological-order list).
// Parsing sub_title here gives a reliable chronological order regardless.
//   "2026-03-09第6-8节" -> { dateNum: 20260309, period: 6 }
function _lectureOrderKey(subTitle) {
  var s = String(subTitle || "");
  var dm = s.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  var dateNum = dm
    ? parseInt(dm[1], 10) * 10000 + parseInt(dm[2], 10) * 100 + parseInt(dm[3], 10)
    : null;
  var pm = s.match(/第\s*(\d+)/);  // first period number, e.g. 第11-12节 -> 11
  var period = pm ? parseInt(pm[1], 10) : 0;
  return { dateNum: dateNum, period: period, raw: s };
}

function _getLectures(courseId) {
  const rows = _queryAll(`
    SELECT sub_id, sub_title, date, summary, processed_at,
           ${_queryAll("PRAGMA table_info(lectures)").some(row => row.name === "ai_title") ? "ai_title" : "NULL AS ai_title"},
           error_stage, error_msg, summary_model, transcript
    FROM lectures WHERE course_id = ?
  `, [courseId]);
  // Chronological ascending (earliest first); 第N-M节 breaks intra-day ties so
  // a morning session sorts before an afternoon one. Lectures with no parseable
  // date in their sub_title sort last (their position is genuinely unknown).
  rows.sort(function (a, b) {
    var ka = _lectureOrderKey(a.sub_title), kb = _lectureOrderKey(b.sub_title);
    if (ka.dateNum === null || kb.dateNum === null) {
      if (ka.dateNum === null && kb.dateNum === null)
        return ka.raw < kb.raw ? -1 : (ka.raw > kb.raw ? 1 : 0);
      return ka.dateNum === null ? 1 : -1;
    }
    if (ka.dateNum !== kb.dateNum) return ka.dateNum - kb.dateNum;
    if (ka.period !== kb.period) return ka.period - kb.period;
    return ka.raw < kb.raw ? -1 : (ka.raw > kb.raw ? 1 : 0);
  });
  return rows.map((r) => {
    r.state = _deriveState(r);
    r.has_summary = r.summary !== null;
    r.transcript_available = Boolean(String(r.transcript || '').trim());
    delete r.transcript;
    return r;
  });
}

function _getLecture(subId) {
  const rows = _queryAll(`
    SELECT l.*, c.title AS course_title, c.teacher
    FROM lectures l JOIN courses c ON l.course_id = c.course_id
    WHERE l.sub_id = ?
  `, [subId]);
  if (!rows.length) return null;
  rows[0].state = _deriveState(rows[0]);
  return rows[0];
}

function _getPptPages(subId) {
  // Only return done pages with non-empty text — keeps the PPT viewer
  // free of pending placeholders and dropped pages.
  return _queryAll(`
    SELECT page_num, created_sec, text
    FROM ppt_pages
    WHERE sub_id = ? AND ocr_status = 'done'
      AND text IS NOT NULL AND text != ''
    ORDER BY created_sec ASC
  `, [subId]);
}

function _searchSummaries(query, courseIds, page = 1, pageSize = 50, domains = {}) {
  const terms = String(query || '').trim().split(/\s+/).filter(Boolean);
  const active = ['title', 'summary', 'transcript', 'ocr'].filter(name => domains[name] !== false);
  page = Math.max(1, Number(page) || 1);
  pageSize = Math.max(1, Math.min(100, Number(pageSize) || 50));
  const empty = {results: [], total: 0, page, hasMore: false};
  if (!terms.length || !active.length) return empty;
  const hasTitle = _queryAll("PRAGMA table_info(lectures)").some(row => row.name === 'ai_title');
  const condition = (domain, term) => {
    const needle = `%${term}%`;
    if (domain === 'title') return hasTitle
      ? {sql: '(l.sub_title LIKE ? OR l.ai_title LIKE ?)', params: [needle, needle]}
      : {sql: 'l.sub_title LIKE ?', params: [needle]};
    if (domain === 'ocr') return {sql: "EXISTS (SELECT 1 FROM ppt_pages p WHERE p.sub_id = l.sub_id AND p.ocr_status = 'done' AND p.text LIKE ?)", params: [needle]};
    return {sql: `l.${domain} LIKE ?`, params: [needle]};
  };
  const where = [], whereParams = [];
  for (const term of terms) {
    const parts = active.map(domain => condition(domain, term));
    where.push('(' + parts.map(part => part.sql).join(' OR ') + ')');
    whereParams.push(...parts.flatMap(part => part.params));
  }
  if (courseIds?.length) {
    where.push(`l.course_id IN (${courseIds.map(() => '?').join(',')})`);
    whereParams.push(...courseIds);
  }
  const cases = [], caseParams = [];
  for (const domain of active) {
    const parts = terms.map(term => condition(domain, term));
    cases.push(`WHEN (${parts.map(part => part.sql).join(' OR ')}) THEN '${domain}'`);
    caseParams.push(...parts.flatMap(part => part.params));
  }
  const pptSql = active.includes('ocr')
    ? `(SELECT p.text FROM ppt_pages p WHERE p.sub_id = l.sub_id AND p.ocr_status = 'done' AND (${terms.map(() => 'p.text LIKE ?').join(' OR ')}) ORDER BY p.page_num LIMIT 1)` : 'NULL';
  const pptParams = active.includes('ocr') ? terms.map(term => `%${term}%`) : [];
  const base = `FROM lectures l JOIN courses c ON l.course_id = c.course_id WHERE ${where.join(' AND ')}`;
  const total = _queryAll(`SELECT COUNT(*) AS total ${base}`, whereParams)[0].total;
  const rows = _queryAll(`SELECT l.sub_id, l.sub_title, l.summary, l.transcript,
    ${hasTitle ? 'l.ai_title' : 'NULL'} AS ai_title, ${pptSql} AS ppt_text,
    l.course_id, c.title AS course_title, CASE ${cases.join(' ')} ELSE 'other' END AS hit_field
    ${base} ORDER BY CASE hit_field WHEN 'title' THEN 0 WHEN 'summary' THEN 1 WHEN 'transcript' THEN 2 ELSE 3 END,
    l.processed_at DESC, l.sub_id DESC LIMIT ? OFFSET ?`,
    [...pptParams, ...caseParams, ...whereParams, pageSize, (page - 1) * pageSize]);
  return {results: rows, total, page, hasMore: page * pageSize < total};
}

function _invalidTermExclusion() {
  // Single source of truth for excluding pre-recording-system terms. Used by
  // both the catalog WHERE builder and the term dropdown so the two never
  // diverge (they previously used different mechanisms — SQL GLOB here vs a
  // JS substring test in the dropdown — which could disagree).
  var clauses = [];
  var params = [];
  for (var gi = 0; gi < _INVALID_TERMS_GLOB.length; gi++) {
    clauses.push("term NOT GLOB ?");
    params.push(_INVALID_TERMS_GLOB[gi]);
  }
  for (var ei = 0; ei < _INVALID_TERMS_EXACT.length; ei++) {
    clauses.push("term != ?");
    params.push(_INVALID_TERMS_EXACT[ei]);
  }
  return { clauses: clauses, params: params };
}

function _getAllCoursesTerms() {
  var ex = _invalidTermExclusion();
  var where = ex.clauses.length ? "WHERE " + ex.clauses.join(" AND ") : "";
  return _queryAll(
    "SELECT DISTINCT term FROM all_courses " + where + " ORDER BY term DESC",
    ex.params,
  ).map(function (r) { return r.term; });
}

function _buildCatalogWhere(filters) {
  // Shared WHERE/params builder for paged search + count + dept distinct.
  // Filters: { terms: string[], depts: string[], title: string, teacher: string }
  var ex = _invalidTermExclusion();
  var clauses = ex.clauses.slice();
  var params = ex.params.slice();
  if (filters.terms && filters.terms.length) {
    clauses.push("term IN (" + filters.terms.map(function () { return "?"; }).join(",") + ")");
    for (var i = 0; i < filters.terms.length; i++) params.push(filters.terms[i]);
  }
  if (filters.depts && filters.depts.length) {
    clauses.push("dept IN (" + filters.depts.map(function () { return "?"; }).join(",") + ")");
    for (var j = 0; j < filters.depts.length; j++) params.push(filters.depts[j]);
  }
  if (filters.title && filters.title.trim()) {
    clauses.push("title LIKE ?");
    params.push("%" + filters.title.trim() + "%");
  }
  if (filters.teacher && filters.teacher.trim()) {
    clauses.push("teacher LIKE ?");
    params.push("%" + filters.teacher.trim() + "%");
  }
  return {
    where: clauses.length ? "WHERE " + clauses.join(" AND ") : "",
    params: params,
  };
}

function _searchAllCourses(filters, limit) {
  // Paged catalog search — used by the subscriptions editor middle column.
  // Pushes all filtering into sqlite so the JS heap never holds the full
  // 20k-row catalog.
  var w = _buildCatalogWhere(filters || {});
  var sql = "SELECT course_id, term, title, teacher, dept FROM all_courses "
          + w.where + " ORDER BY term DESC, title LIMIT ?";
  var p = w.params.slice();
  p.push(limit || 200);
  return _queryAll(sql, p);
}

function _countAllCourses(filters) {
  var w = _buildCatalogWhere(filters || {});
  var rows = _queryAll("SELECT COUNT(*) AS n FROM all_courses " + w.where, w.params);
  return rows.length ? rows[0].n : 0;
}

function _getCoursesByIds(ids) {
  // Look up the catalog rows for an arbitrary set of course_ids.  Used by
  // the left ("已订阅") and right ("单次运行") columns so they can render
  // without holding the full catalog in JS.  Deduplicates by course_id;
  // a course offered in multiple terms is shown with its most recent term.
  if (!ids || !ids.length) return [];
  var placeholders = ids.map(function () { return "?"; }).join(",");
  // Pull every term row that matches, then collapse to one per course_id
  // (preferring the most recent term) in JS — keeps the SQL simple.
  var rows = _queryAll(
    "SELECT course_id, term, title, teacher, dept FROM all_courses "
    + "WHERE course_id IN (" + placeholders + ") ORDER BY term DESC",
    ids.map(String),
  );
  var seen = {};
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var cid = String(rows[i].course_id);
    if (seen[cid]) continue;
    seen[cid] = true;
    out.push(rows[i]);
  }
  // Synthesize empty placeholders for IDs the catalog doesn't know about,
  // so the count shown to the user matches what's actually in their list.
  var foundIds = {};
  for (var k = 0; k < out.length; k++) foundIds[String(out[k].course_id)] = true;
  for (var m = 0; m < ids.length; m++) {
    var idStr = String(ids[m]);
    if (!foundIds[idStr]) {
      out.push({ course_id: idStr, term: "", title: "", teacher: "", dept: "" });
      foundIds[idStr] = true;
    }
  }
  return out;
}

function _getAllCoursesDepts(termFilter, search) {
  // Distinct dept list, optionally narrowed to a set of terms and a
  // case-insensitive substring search.  Lets the dept dropdown stay
  // responsive without iterating the JS catalog.
  var clauses = ["dept IS NOT NULL", "dept != ''"];
  var params = [];
  if (termFilter && termFilter.length) {
    clauses.push("term IN (" + termFilter.map(function () { return "?"; }).join(",") + ")");
    for (var i = 0; i < termFilter.length; i++) params.push(termFilter[i]);
  }
  if (search && search.trim()) {
    clauses.push("LOWER(dept) LIKE ?");
    params.push("%" + search.trim().toLowerCase() + "%");
  }
  var rows = _queryAll(
    "SELECT DISTINCT dept FROM all_courses WHERE "
    + clauses.join(" AND ") + " ORDER BY dept",
    params,
  );
  return rows.map(function (r) { return r.dept; });
}

function _getMeta(key) {
  var rows = _queryAll("SELECT value FROM meta WHERE key = ?", [key]);
  return rows.length ? rows[0].value : null;
}

window.ICS.db = {
  close: () => { if (_db) _db.close(); _db = null; },
  queryAll: _queryAll,
  getSummaryVersions: subId => _queryAll("SELECT model, summary, generated_at FROM summary_versions WHERE sub_id = ? ORDER BY generated_at DESC, model", [subId]),
  initDB: _initFromBytes,
  initEmpty: _initEmpty,
  attachShard: _attachShard,
  exportDB: _exportDB,
  getCourses: _getCourses,
  getLectures: _getLectures,
  getLecture: _getLecture,
  getPptPages: _getPptPages,
  searchSummaries: _searchSummaries,
  getAllCoursesTerms: _getAllCoursesTerms,
  searchAllCourses: _searchAllCourses,
  countAllCourses: _countAllCourses,
  getCoursesByIds: _getCoursesByIds,
  getAllCoursesDepts: _getAllCoursesDepts,
  getMeta: _getMeta,
};
