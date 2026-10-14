/* Keep defaults/validation aligned with src/ai/model_policy.py. */
(() => {
  const defaults = {
    timeout_seconds:600, title_timeout_seconds:60, max_retries:2,
    retry_delay_seconds:2, retry_max_delay_seconds:60,
    output_tokens:32768, max_output_tokens:65536, token_parameter:'auto',
  };
  function normalize(value = {}) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('生成设置必须是 object');
    if (Object.keys(value).some(key => !(key in defaults))) throw new Error('存在未知生成设置');
    const policy = {...defaults, ...value};
    for (const [key, minimum, maximum] of [
      ['timeout_seconds',1,86400], ['title_timeout_seconds',1,86400],
      ['max_retries',0,10], ['retry_delay_seconds',0,3600], ['retry_max_delay_seconds',0,3600],
      ['output_tokens',1,2147483647], ['max_output_tokens',1,2147483647],
    ]) {
      if (!Number.isInteger(policy[key]) || policy[key] < minimum || policy[key] > maximum) throw new Error(`${key} 必须为 ${minimum}–${maximum} 的整数`);
    }
    if (policy.max_output_tokens < policy.output_tokens) throw new Error('扩容上限不能小于初始输出预算');
    if (policy.retry_max_delay_seconds < policy.retry_delay_seconds) throw new Error('退避上限不能小于初始重试间隔');
    if (!['auto','max_tokens','max_completion_tokens'].includes(policy.token_parameter)) throw new Error('输出预算参数无效');
    return policy;
  }
  window.ICourseGenerationPolicy = {normalize};
})();
