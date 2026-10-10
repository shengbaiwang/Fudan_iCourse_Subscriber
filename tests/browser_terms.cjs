/* SQL.js regression: SQLJS_DIR must contain sql-wasm.js and sql-wasm.wasm. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

(async () => {
  const root = path.resolve(__dirname, '..');
  const sqlDir = process.env.SQLJS_DIR;
  if (!sqlDir) throw new Error('Set SQLJS_DIR to the sql.js dist directory');
  const SQL = await require(path.join(sqlDir, 'sql-wasm.js'))({locateFile: name => path.join(sqlDir, name)});
  const context = {window: {initSqlJs: async () => SQL}};
  for (const name of ['terms.js', 'departments.js', 'browser/schema.js', 'browser/db.js']) {
    vm.runInNewContext(fs.readFileSync(path.join(root, 'local_web/static', name), 'utf8'), context);
  }
  const {formatTerm} = context.window.ICS;
  for (const [raw, display] of [
    ['2025-20261', '2025 秋'], ['2025–2026 秋季', '2025 秋'],
    ['2025-20262', '2026 春'], ['2025–2026 第二学期', '2026 春'],
    ['2025-2026暑期', '2026 暑'], ['2030-2031学年暑期学期', '2031 暑'],
    [' 2030 — 2031 第1学期 ', '2030 秋'], ['2030-2031-2', '2031 春'],
    ['2026-秋', '2026 秋'], ['2026-春', '2026 春'], ['2026 暑期学期', '2026 暑'],
    [null, ''], ['', ''], ['未知学期', '未知学期'], ['2030-20321', '2030-20321'],
  ]) {
    assert.equal(formatTerm(raw), display);
    assert.equal(formatTerm(display), display);
  }
  const api = context.window.ICS.db;
  const legacy = new SQL.Database();
  legacy.exec(context.window.ICS.schema.SCHEMA_SQL);
  legacy.run("INSERT INTO courses VALUES ('1', '课程', '教师')");
  legacy.run("INSERT INTO all_courses VALUES "
    + "('1','2025-2026-1','课程','教师','院系','old'),"
    + "('1','2025-20262','课程','教师','院系','old'),"
    + "('2','2025-20261','另一课程','教师','院系','old'),"
    + "('3','2025-2026暑期','暑期课程','教师','院系','old')");
  const bytes = legacy.export();
  legacy.close();
  const check = () => {
    assert.deepEqual(Array.from(api.getAllCoursesTerms()), ['2025–2026 暑期', '2025–2026 春季', '2025–2026 秋季']);
    assert.equal(api.getCourses()[0].term, '2025–2026 春季');
    assert.equal(api.getCoursesByIds(['1'])[0].term, '2025–2026 春季');
    for (const term of ['2025–2026 秋季', '2025–2026 第一学期', '2025-2026-1', '2025-20261']) {
      assert.equal(api.countAllCourses({terms: [term]}), 2);
      assert.equal(api.searchAllCourses({terms: [term]}, 1)[0].term, '2025–2026 秋季');
      assert.deepEqual(Array.from(api.getAllCoursesDepts([term])), ['院系']);
    }
    assert.equal(api.queryAll("SELECT term FROM all_courses WHERE course_id = '2'")[0].term, '2025-20261');
  };
  await api.initDB(bytes);
  check();
  const cached = api.exportDB();
  // Export/cache reload and attaching old shards must keep the formatter active.
  await api.initDB(cached);
  check();
  await api.initEmpty();
  await api.attachShard(bytes);
  check();
  api.queryAll("INSERT INTO all_courses VALUES ('future','2030-20311','新课程','教师','院系','new')");
  assert.equal(api.getAllCoursesTerms()[0], '2030–2031 秋季');
  assert.equal(api.countAllCourses({terms: ['2030–2031 秋季']}), 1);
  api.close();
  console.log('Browser term display and queries passed (calendar years, legacy, cache, shards, future imports).');
})().catch(error => { console.error(error); process.exitCode = 1; });
