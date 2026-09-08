/* Browser implementation of the console API. No second view/controller lives here. */
(() => {
  const {github, crypto, db} = window.ICS;
  let credentials = null;
  let repository = readJSON('ics_settings', null) || github.detectRepo() || {};
  repository = {owner: repository.owner || '', repo: repository.repo || '', branch: repository.branch || 'data'};
  let ready = false;
  let commitSha = null;
  let syncing = null;
  const encryptedCache = new Map();

  function readJSON(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (_) { return fallback; }
  }
  const scopedKey = name => `icourse:${repository.owner}/${repository.repo}/${repository.branch}:${name}`;
  const readPreference = (name, fallback) => readJSON(scopedKey(name), fallback);
  const savePreference = (name, value) => localStorage.setItem(scopedKey(name), JSON.stringify(value));
  const ids = values => [...new Set((values || []).map(value => String(value).trim()).filter(Boolean))];
  const query = (sql, params = []) => db.queryAll(sql, params);

  async function gh(path, options = {}, allowMissing = false) {
    if (!credentials) throw new Error('请先连接 GitHub 仓库');
    const response = await fetch(`https://api.github.com/repos/${encodeURIComponent(repository.owner)}/${encodeURIComponent(repository.repo)}${path}`, {
      ...options,
      headers: {Authorization: `Bearer ${credentials.token}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json'},
    });
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) {
      let detail = '';
      try { detail = (await response.json()).message || ''; } catch (_) {}
      throw new Error(`GitHub ${response.status}：${detail || response.statusText}`);
    }
    return response.status === 204 ? null : response.json();
  }
  async function blob(entry) {
    if (!encryptedCache.has(entry.sha)) {
      encryptedCache.set(entry.sha, await github.fetchBlobBytes(repository.owner, repository.repo, entry.sha, credentials.token));
    }
    return encryptedCache.get(entry.sha);
  }
  async function gunzip(bytes) {
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
  }
  async function synchronize() {
    if (syncing) return syncing;
    syncing = (async () => {
      const manifest = await github.fetchShardManifest(repository.owner, repository.repo, repository.branch, credentials.token);
      if (ready && commitSha === manifest.commitSha) return {unchanged: true};
      // Preserve the last usable DB if a shard/network/decryption operation fails.
      const previous = ready ? db.exportDB() : null;
      try {
        if (manifest.format === 'sharded') {
          const password = await crypto.buildPassword(credentials);
          const indexBytes = await crypto.decrypt(await blob(manifest.index), password);
          if (!crypto.isJsonObj(indexBytes)) throw new Error('解密失败，请检查学号和 UIS 密码');
          const index = JSON.parse(new TextDecoder().decode(indexBytes));
          if (!Array.isArray(index.shards)) throw new Error('分片索引格式错误');
          await db.initEmpty();
          for (const shard of index.shards) {
            const entry = manifest.shards.find(item => item.name === shard.name);
            if (!entry) throw new Error(`资料库缺少分片：${shard.name}`);
            const compressed = await crypto.decrypt(await blob(entry), password);
            if (!crypto.isGzip(compressed)) throw new Error(`无法解密分片：${shard.name}`);
            const bytes = await gunzip(compressed);
            if (!crypto.isSqlite(bytes)) throw new Error(`分片不是 SQLite 数据库：${shard.name}`);
            await db.attachShard(bytes);
          }
        } else {
          const result = await crypto.decryptWithFallback(await blob(manifest.legacy), credentials,
            manifest.legacy.compressed ? crypto.isGzip : crypto.isSqlite);
          const bytes = manifest.legacy.compressed ? await gunzip(result.data) : result.data;
          if (!crypto.isSqlite(bytes)) throw new Error('解密结果不是 SQLite 数据库');
          await db.initDB(bytes);
        }
        ready = true;
        commitSha = manifest.commitSha;
        return {unchanged: false};
      } catch (error) {
        if (previous) await db.initDB(previous);
        else db.close();
        throw new Error(error.message || '解密失败，请检查学号、UIS 密码及旧版密钥字段');
      }
    })();
    try { return await syncing; } finally { syncing = null; }
  }
  function status() {
    const counts = ready ? query(`SELECT COUNT(*) AS lectures,
      SUM(summary IS NOT NULL) AS ready, SUM(error_stage IS NOT NULL AND error_stage != 'no_video') AS failed FROM lectures`)[0] : null;
    return {configured: !!credentials, repository, keychain_available: false, database_ready: ready,
      database: ready ? {...counts, courses: db.getCourses().length, commit_sha: commitSha} : null,
      update: {state: syncing ? 'checking' : ready ? 'current' : 'idle'}};
  }
  function subscriptions() {
    const saved = readPreference('subscriptions', null);
    const courseIds = saved === null ? ids((db.getMeta('subscribed_course_ids') || db.getMeta('course_ids') || '').split(',')) : saved;
    return {course_ids: courseIds, courses: db.getCoursesByIds(courseIds), source: saved === null ? 'data-snapshot' : 'browser-save'};
  }
  async function secretNames() {
    const names = new Set();
    for (let page = 1; ; page++) {
      const data = await gh(`/actions/secrets?per_page=100&page=${page}`);
      for (const secret of data.secrets) names.add(secret.name);
      if (data.secrets.length < 100) return names;
    }
  }
  let sodiumPromise;
  async function sodiumReady() {
    if (!sodiumPromise) sodiumPromise = (async () => {
      for (const url of ['libsodium@0.7.15/dist/modules/libsodium.min.js', 'libsodium-wrappers@0.7.15/dist/modules/libsodium-wrappers.min.js']) {
        await new Promise((resolve, reject) => {
          const node = document.createElement('script');
          node.src = `https://cdn.jsdelivr.net/npm/${url}`;
          node.onload = resolve;
          node.onerror = () => reject(new Error('无法加载 Secret 加密组件，请重试'));
          document.head.append(node);
        });
      }
      await window.sodium.ready;
      return window.sodium;
    })().catch(error => { sodiumPromise = null; throw error; });
    return sodiumPromise;
  }
  async function setSecret(name, value) {
    const sodium = await sodiumReady();
    const pub = await github.getRepoPublicKey(repository.owner, repository.repo, credentials.token);
    const cipher = sodium.crypto_box_seal(sodium.from_string(value), sodium.from_base64(pub.key, sodium.base64_variants.ORIGINAL));
    await github.putRepoSecret(repository.owner, repository.repo, credentials.token, name,
      sodium.to_base64(cipher, sodium.base64_variants.ORIGINAL), pub.key_id);
  }
  function validateProviders(document) {
    if (Array.isArray(document)) document = {version: 1, providers: document};
    if (![undefined, 1, '1'].includes(document?.version)) throw new Error('不支持的模型配置版本');
    const rows = document?.providers;
    if (!Array.isArray(rows) || !rows.length || rows.length > 20) throw new Error('需要 1–20 个供应商');
    const names = new Set();
    const providers = rows.map(row => {
      const name = String(row.name || '').trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$/.test(name) || names.has(name)) throw new Error(`供应商名称无效或重复：${name}`);
      names.add(name);
      const env = String(row.api_key_env || '').trim().toUpperCase();
      if (!['DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY', 'GEMINI_API_KEY'].includes(env) && !/^LLM_[A-Z0-9_]{1,80}_API_KEY$/.test(env)) throw new Error(`${name} 的 Secret 名称无效`);
      const base = String(row.base_url || row.default_base_url || '').trim().replace(/\/+$/, '');
      let url;
      try { url = new URL(base); } catch (_) { throw new Error(`${name} 的 Base URL 无效`); }
      if (url.protocol !== 'https:' || !url.hostname || url.username || url.password || /[?#\\\s]/.test(base)) throw new Error(`${name} 需要无凭据和查询参数的 HTTPS Base URL`);
      if (!Array.isArray(row.models) || row.models.some(model => typeof model !== 'string')) throw new Error(`${name} 的模型列表无效`);
      const models = ids(row.models);
      if (!models.length || models.length > 30 || models.some(model => model.length > 200)) throw new Error(`${name} 需要 1–30 个有效模型`);
      if (row.enabled !== undefined && typeof row.enabled !== 'boolean') throw new Error('enabled 必须是布尔值');
      const provider = {name, api_key_env: env, default_base_url: base, models, enabled: row.enabled !== false};
      if (row.base_url_env) {
        const baseEnv = String(row.base_url_env).trim().toUpperCase();
        if (!['DASHSCOPE_BASE_URL', 'DEEPSEEK_BASE_URL', 'GEMINI_BASE_URL'].includes(baseEnv) && !/^LLM_[A-Z0-9_]{1,80}_BASE_URL$/.test(baseEnv)) throw new Error('Base URL 环境变量名称无效');
        provider.base_url_env = baseEnv;
      }
      return provider;
    });
    if (!providers.some(row => row.enabled)) throw new Error('至少需要启用一个供应商');
    return {version: 1, providers};
  }
  async function providers() {
    const raw = await gh('/actions/variables/MODEL_PROVIDERS_JSON', {}, true);
    const defaults = raw ? null : await fetch('browser/default-providers.json').then(response => {
      if (!response.ok) throw new Error('无法加载默认模型配置');
      return response.json();
    });
    const document = validateProviders(raw ? JSON.parse(raw.value) : defaults);
    const names = await secretNames();
    return {version: 1, source: raw ? 'github-variable' : 'defaults', providers: document.providers.map(row => ({
      ...row, base_url: row.default_base_url, api_key_configured: names.has(row.api_key_env),
    }))};
  }
  async function saveProviders(payload) {
    const document = validateProviders(payload);
    const names = await secretNames();
    const keys = new Map();
    payload.providers.forEach((row, index) => {
      const value = String(row.api_key || '').trim();
      const env = document.providers[index].api_key_env;
      if (value && keys.has(env) && keys.get(env) !== value) throw new Error(`同一 Secret 收到不同的 Key：${env}`);
      if (value) keys.set(env, value);
    });
    for (const row of document.providers) {
      if (row.enabled && !names.has(row.api_key_env) && !keys.has(row.api_key_env)) throw new Error(`${row.name} 尚未配置 API Key`);
    }
    for (const [env, value] of keys) await setSecret(env, value);
    const old = await gh('/actions/variables/MODEL_PROVIDERS_JSON', {}, true);
    await gh(old ? '/actions/variables/MODEL_PROVIDERS_JSON' : '/actions/variables', {
      method: old ? 'PATCH' : 'POST', body: JSON.stringify({name: 'MODEL_PROVIDERS_JSON', value: JSON.stringify(document)}),
    });
    return providers();
  }
  async function dispatch(workflow, payload) {
    if (!['check.yml', 'single_run.yml', 'export.yml', 'delete_course.yml', 'deploy-frontend.yml'].includes(workflow)) throw new Error('不允许触发该 workflow');
    await gh(`/actions/workflows/${workflow}/dispatches`, {method: 'POST', body: JSON.stringify({ref: payload.ref || 'main', inputs: payload.inputs || {}})});
    return {ok: true};
  }
  async function rerun(payload) {
    const subIds = ids(payload.sub_ids);
    for (const courseId of ids(payload.course_ids)) {
      for (const row of db.getLectures(courseId)) if (row.transcript_available && !subIds.includes(String(row.sub_id))) subIds.push(String(row.sub_id));
    }
    if (!subIds.length || subIds.length > 20) throw new Error(`已选择 ${subIds.length} 个课次；一次请选择 1–20 个有转录的课次`);
    for (const id of subIds) {
      if (id.length > 100 || id.includes(',') || !db.getLecture(id)?.transcript?.trim()) throw new Error(`课次没有可用转录或 ID 无效：${id}`);
    }
    const config = await providers();
    const provider = config.providers.find(row => row.name === payload.provider && row.enabled && row.api_key_configured);
    if (!provider?.models.includes(payload.model)) throw new Error('所选模型未启用或尚未配置 Key');
    await dispatch('single_run.yml', {inputs: {course_ids: '', resummarize_sub_ids: subIds.join(','), summary_provider: payload.provider, summary_model: payload.model, use_official_transcript: 'false'}});
    return {ok: true, sub_ids: subIds, count: subIds.length};
  }
  async function request(path, options = {}) {
    const url = new URL(path, location.origin);
    const route = url.pathname.replace(/^\/api\/local/, '');
    const method = options.method || 'GET';
    const payload = options.body ? JSON.parse(options.body) : {};
    if (route === '/status') return status();
    if (route === '/configure' && method === 'POST') {
      if (syncing) throw new Error('请等待当前同步完成后再切换仓库');
      const owner = String(payload.owner || '').trim(), repo = String(payload.repo || '').trim();
      if (!/^[\w.-]+$/.test(owner) || !/^[\w.-]+$/.test(repo) || !payload.token?.trim() || !payload.stuid || !payload.uispsw) throw new Error('请填写仓库、Token、学号和 UIS 密码');
      const old = {credentials, repository, ready, commitSha};
      credentials = {token: payload.token.trim(), stuid: payload.stuid, uispsw: payload.uispsw, dashscope: payload.dashscope || '', smtp: payload.smtp || ''};
      repository = {owner, repo, branch: String(payload.branch || 'data').trim()};
      // Validate both authorization and decryption even if the commit is unchanged.
      ready = false;
      const previous = old.ready ? db.exportDB() : null;
      encryptedCache.clear();
      try { await gh(''); await synchronize(); }
      catch (error) {
        credentials = old.credentials; repository = old.repository; ready = old.ready; commitSha = old.commitSha;
        if (previous) await db.initDB(previous);
        throw error;
      }
      const savedRepository = readJSON('ics_settings', null);
      const oldSubscriptions = readJSON('ics_lastSubscribed', null);
      if (savedRepository?.owner === owner && savedRepository?.repo === repo &&
          (savedRepository.branch || 'data') === repository.branch &&
          readPreference('subscriptions', null) === null && Array.isArray(oldSubscriptions)) {
        savePreference('subscriptions', ids(oldSubscriptions));
      }
      localStorage.setItem('ics_settings', JSON.stringify(repository));
      // Remove credentials left by the former viewer; do not persist the new session.
      localStorage.removeItem('ics_creds');
      return {ok: true};
    }
    if (route === '/credentials/forget' && method === 'POST') {
      if (syncing) throw new Error('请等待同步完成后再退出会话');
      credentials = null; ready = false; commitSha = null; encryptedCache.clear(); db.close();
      localStorage.removeItem('ics_creds');
      return {ok: true};
    }
    if (!credentials) throw new Error('请先连接 GitHub 仓库');
    if (route === '/sync' && method === 'POST') return synchronize();
    if (route === '/workflows') return (await gh('/actions/runs?per_page=10')).workflow_runs;
    const workflow = /^\/workflows\/([^/]+)\/dispatch$/.exec(route);
    if (workflow && method === 'POST') return dispatch(workflow[1], payload);
    if (route === '/model-providers') return method === 'PUT' ? saveProviders(payload) : providers();
    if (route === '/summary-reruns' && method === 'POST') return rerun(payload);
    const singleRerun = /^\/lectures\/([^/]+)\/rerun-summary$/.exec(route);
    if (singleRerun && method === 'POST') return rerun({...payload, sub_ids: [decodeURIComponent(singleRerun[1])]});
    if (route === '/course-zones') {
      let zones = readPreference('zones', {});
      if (method === 'PUT') {
        if (!['organize', 'study', 'reference', 'archive'].includes(payload.zone)) throw new Error('未知课程分区');
        zones = {...zones, [String(payload.course_id)]: payload.zone};
        savePreference('zones', zones);
      }
      return {zones};
    }
    if (syncing) {
      try { await syncing; } catch (error) { if (!ready) throw error; }
    }
    if (!ready) throw new Error('请先检查更新，打开资料库');
    if (route === '/courses') return db.getCourses();
    const course = /^\/courses\/([^/]+)\/lectures$/.exec(route);
    if (course) return db.getLectures(decodeURIComponent(course[1]));
    const lecture = /^\/lectures\/([^/]+)$/.exec(route);
    if (lecture) {
      const row = db.getLecture(decodeURIComponent(lecture[1]));
      if (!row) throw new Error('课次不存在');
      return {...row, ppt_pages: db.getPptPages(row.sub_id), summary_versions: db.getSummaryVersions(row.sub_id)};
    }
    if (route === '/search') {
      const q = url.searchParams.get('q') || '';
      const page = Number(url.searchParams.get('page') || 1);
      const domains = Object.fromEntries(['summary', 'transcript', 'ocr'].map(name => [name, url.searchParams.get(name) !== 'false']));
      return db.searchSummaries(q, ids((url.searchParams.get('courses') || '').split(',')), page, 50, domains).results.map(row => {
        const value = String(row.hit_field === 'ocr' ? row.ppt_text || '' : row[row.hit_field] || '');
        const start = Math.max(0, value.toLowerCase().indexOf(q.toLowerCase()) - 70);
        return {...row, snippet: value.slice(start, start + 240)};
      });
    }
    if (route === '/subscriptions') {
      if (method === 'PUT') {
        const courseIds = ids(payload.course_ids);
        if (courseIds.some(id => id.length > 100 || id.includes(','))) throw new Error('课程 ID 格式不正确');
        await setSecret('COURSE_IDS', courseIds.join(','));
        savePreference('subscriptions', courseIds);
      }
      return subscriptions();
    }
    if (route === '/subscription-catalog') {
      const q = `%${(url.searchParams.get('q') || '').trim()}%`;
      const term = url.searchParams.get('term') || '';
      return {terms: db.getAllCoursesTerms(), courses: query(`SELECT course_id, title, teacher, term, dept FROM all_courses
        WHERE term NOT GLOB '*_19_*' AND term != '25' AND (title LIKE ? OR teacher LIKE ? OR dept LIKE ? OR course_id LIKE ?)
        ${term ? 'AND term = ?' : ''} ORDER BY term DESC, title LIMIT 100`, [q, q, q, q, ...(term ? [term] : [])])};
    }
    throw new Error('这项操作需要启动本地控制台');
  }
  window.ICOURSE_API = request;
})();
