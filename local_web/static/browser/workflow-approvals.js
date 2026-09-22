/* GitHub approval only; never rerun a failed job or change repository policies. */
(() => {
  const workflows = new Set(['.github/workflows/single_run.yml']);
  function eligible(run, owner, repo, now) {
    const age = now - Date.parse(run.created_at);
    const same = name => typeof name === 'string' && name.toLowerCase() === `${owner}/${repo}`.toLowerCase();
    return Number.isSafeInteger(run.id) && run.id > 0 && run.status === 'completed'
      && run.conclusion === 'action_required' && run.run_attempt === 1
      && run.event === 'workflow_dispatch' && run.head_branch === 'main'
      && workflows.has(run.path) && Array.isArray(run.pull_requests) && !run.pull_requests.length
      && same(run.repository?.full_name) && same(run.head_repository?.full_name)
      && [run.actor, run.triggering_actor].every(actor => actor?.login?.toLowerCase() === owner.toLowerCase())
      && age >= 0 && age <= 86400000;
  }
  async function reconcile(request, owner, repo, canApprove = () => true, now = Date.now()) {
    const base = `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`;
    const rows = (await request(`${base}/actions/runs?branch=main&per_page=100`)).workflow_runs;
    const pending = rows.filter(run => eligible(run, owner, repo, now));
    const result = {approved: [], errors: []};
    if (!pending.length || (await request('/user')).login.toLowerCase() !== owner.toLowerCase()) return result;
    for (const row of pending) {
      try {
        const run = await request(`${base}/actions/runs/${row.id}`);
        if (run.id !== row.id || !eligible(run, owner, repo, now)) continue;
        const head = (await request(`${base}/git/ref/heads/main`)).object.sha;
        if (!head || run.head_sha !== head || !canApprove()) continue;
        await request(`${base}/actions/runs/${run.id}/approve`, {method: 'POST'});
        result.approved.push(run.id);
      } catch (error) { result.errors.push({id: row.id, message: error.message}); }
    }
    return result;
  }
  if (typeof module !== 'undefined') module.exports = {eligible, reconcile};
  else window.ICourseWorkflowApprovals = {eligible, reconcile};
})();
