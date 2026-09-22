/* Offline parity checks using the same URL cases as the Python runtime. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const context = {window: {}, URL};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../local_web/static/provider-urls.js'), 'utf8'), context);
const {normalize} = context.window.ICourseProviderURLs;
for (const [value, expected] of JSON.parse(fs.readFileSync(path.join(__dirname, 'provider_url_cases.json'), 'utf8'))) {
  assert.equal(normalize(value), expected);
  assert.equal(normalize(expected), expected);
}
for (const invalid of ['http://api.xiaomimimo.com', 'https://user:secret@api.xiaomimimo.com', 'https://api.xiaomimimo.com/?key=secret']) {
  assert.equal(normalize(invalid), invalid);
}
console.log('Provider URL normalization parity passed.');
