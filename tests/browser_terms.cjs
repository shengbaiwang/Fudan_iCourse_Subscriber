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
    assert.deepEqual(Array.from(api.getAllCoursesTerms()), ['2025–2026 暑期', '2025–2026 第二学期', '2025–2026 第一学期']);
    assert.equal(api.getCourses()[0].term, '2025–2026 第二学期');
    assert.equal(api.getCoursesByIds(['1'])[0].term, '2025–2026 第二学期');
    for (const term of ['2025–2026 第一学期', '2025-2026-1', '2025-20261']) {
      assert.equal(api.countAllCourses({terms: [term]}), 2);
      assert.equal(api.searchAllCourses({terms: [term]}, 1)[0].term, '2025–2026 第一学期');
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
  assert.equal(api.getAllCoursesTerms()[0], '2030–2031 第一学期');
  assert.equal(api.countAllCourses({terms: ['2030–2031 第一学期']}), 1);
  api.close();
  console.log('Browser term queries passed (legacy, cache, shards, future imports).');
})().catch(error => { console.error(error); process.exitCode = 1; });
