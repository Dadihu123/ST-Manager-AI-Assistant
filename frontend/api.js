/**
 * 后端调用封装。
 *
 * 硬约束 F1：`api.api()` 返回的 `data.success` 才是成败判据（HTTP 200 也可能是业务失败），
 * 因此这里统一把失败转成异常，视图层只处理成功路径与 catch。
 */

export function createClient(api) {
  async function call(path, options) {
    const data = await api.api(path, options || {});
    if (!data || data.success !== true) {
      const message = (data && (data.msg || data.error)) || '请求失败';
      const error = new Error(message);
      error.code = (data && data.error) || 'request_failed';
      error.detail = (data && data.detail) || {};
      error.payload = data;
      throw error;
    }
    return data;
  }

  return {
    call,

    status: () => call('/status'),
    health: () => call('/health'),
    cards: (query = '') => call(`/cards${query ? `?q=${encodeURIComponent(query)}` : ''}`),

    profiles: () => call('/profiles'),
    profile: (id) => call(`/profiles/${encodeURIComponent(id)}`),
    saveProfile: (profile) => call('/profiles', { method: 'POST', body: { profile } }),
    deleteProfile: (id) => call(`/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    duplicateProfile: (id, name = '') =>
      call(`/profiles/${encodeURIComponent(id)}/duplicate`, { method: 'POST', body: { name } }),
    setActiveProfile: (id) => call('/profiles/active', { method: 'POST', body: { profile_id: id } }),
    resetProfiles: () => call('/profiles/reset', { method: 'POST' }),
    exportProfiles: () => call('/profiles/export'),
    importProfiles: (payload) => call('/profiles/import', { method: 'POST', body: { payload } }),

    providers: () => call('/providers'),
    saveProvider: (provider) => call('/providers', { method: 'POST', body: { provider } }),
    deleteProvider: (id) => call(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' }),
    setDefaultProvider: (id) =>
      call(`/providers/${encodeURIComponent(id)}/default`, { method: 'POST' }),
    saveProviderSecrets: (id, secrets) =>
      call(`/providers/${encodeURIComponent(id)}/secrets`, { method: 'POST', body: secrets }),
    clearProviderSecrets: (id) =>
      call(`/providers/${encodeURIComponent(id)}/secrets`, { method: 'DELETE' }),
    testProvider: (id, model = '') =>
      call(`/providers/${encodeURIComponent(id)}/test`, { method: 'POST', body: { model } }),

    preview: (payload) => call('/prompt/preview', { method: 'POST', body: payload }),
    validateTemplates: (templates) =>
      call('/prompt/validate', { method: 'POST', body: { templates } }),

    submitAnalyze: (payload) => call('/jobs/analyze', { method: 'POST', body: payload }),
    job: (runId) => call(`/jobs/${encodeURIComponent(runId)}`),
    applyJob: (runId, payload = {}) =>
      call(`/jobs/${encodeURIComponent(runId)}/apply`, { method: 'POST', body: payload }),
    cancelJob: (runId) => call(`/jobs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
    retryJob: (runId, onlyFailed = true) =>
      call(`/jobs/${encodeURIComponent(runId)}/retry`, { method: 'POST', body: { only_failed: onlyFailed } }),

    reviewItem: (runId, itemKey, status) =>
      call(`/jobs/${encodeURIComponent(runId)}/items/${encodeURIComponent(itemKey)}/review`, {
        method: 'POST',
        body: { status },
      }),

    suggestions: () => call('/suggestions'),
    clearSuggestions: (names) => call('/suggestions/clear', { method: 'POST', body: { names } }),

    usage: () => call('/usage'),
    ackOutbound: () => call('/outbound/ack', { method: 'POST' }),
  };
}
