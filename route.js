// CodexDesktopDeepSeekFlashPatch: local provider routing and model-specific defaults.
function codexPatchModelRoutes() {
  return /* MODEL_ROUTES_BEGIN */{"deepseek-flash":{"provider":"deepseek-desktop"},"grok-4.5":{"provider":"easycli-desktop","context_window":500000,"max_context_window":500000,"compact_limit":450000},"grok-4.7":{"provider":"easycli-desktop","context_window":500000,"max_context_window":500000,"compact_limit":450000},"gemini-3.8-flash-high":{"provider":"easycli-desktop","context_window":1048576,"max_context_window":1048576,"compact_limit":943718},"claude-opus-4-6-thinking":{"provider":"easycli-desktop","context_window":1000000,"max_context_window":1000000,"compact_limit":900000}}/* MODEL_ROUTES_END */;
}
function codexPatchAppServerParams(method, params, hostId) {
  if (hostId !== 'local' || !params || typeof params !== 'object') return params;
  if (method === 'thread/list' && params.modelProviders == null)
    return {...params, modelProviders: []};
  if (method !== 'thread/start') return params;
  const routes = codexPatchModelRoutes();
  const route = Object.prototype.hasOwnProperty.call(routes, params.model) ? routes[params.model] : null;
  if (!route) return params;
  const provider = route.provider;
  if (params.modelProvider != null && params.modelProvider !== 'openai' &&
      !(route.context_window && params.modelProvider === provider)) return params;
  const config = {...params.config, web_search: 'disabled'};
  if (route.context_window) {
    const requested = config.model_context_window;
    const window = Number.isSafeInteger(requested) && requested > 0
      ? Math.min(requested, route.max_context_window) : route.context_window;
    config.model_context_window = window;
    const compact = Math.min(route.compact_limit, Math.floor(window * 0.9));
    const requestedCompact = config.model_auto_compact_token_limit;
    config.model_auto_compact_token_limit = Number.isSafeInteger(requestedCompact) && requestedCompact > 0
      ? Math.min(requestedCompact, compact) : compact;
  }
  return {...params, modelProvider: provider, serviceTier: null, config};
}

// Refreshed with the native catalog; used only when returning from a custom provider.
function codexPatchNativeContexts() {
  return /* NATIVE_CONTEXT_BEGIN */{}/* NATIVE_CONTEXT_END */;
}
const codexPatchThreadStates = new WeakMap();
function codexPatchManagedRequest(client, method, params, options) {
  if (client.hostId !== 'local' || !params || typeof params !== 'object') return null;
  let states = codexPatchThreadStates.get(client);
  if (!states) { states = new Map(); codexPatchThreadStates.set(client, states); }
  const id = params.threadId;
  if (id && ['thread/resume', 'thread/unsubscribe'].includes(method)) states.delete(id);
  if (method !== 'turn/start' || typeof id !== 'string') return null;
  const model = params.model ?? params.collaborationMode?.settings?.model;
  const routes = codexPatchModelRoutes();
  const route = Object.prototype.hasOwnProperty.call(routes, model) ? routes[model] : null;
  const native = codexPatchNativeContexts();
  const nativeInfo = Object.prototype.hasOwnProperty.call(native, model) ? native[model] : null;
  if (!route && !nativeInfo) return null;
  const target = route?.provider ?? 'openai';
  const key = JSON.stringify([model, route]);
  const send = (managed = true) => client.enqueueRequest(method, route && managed ? {...params, serviceTier: null, serviceTierForTurn: null} : params, options);
  const known = states.get(id);
  if (known?.key === key) return send(!known.passthrough);
  if (known?.pending) return known.pending.then(() => codexPatchManagedRequest(client, method, params, options));
  const pending = (async () => {
    const read = await client.enqueueRequest('thread/read', {threadId: id, includeTurns: false}, options);
    const thread = read.thread;
    if (!thread || thread.id !== id) throw new Error('Cannot read this chat before switching models.');
    const managedProviders = new Set(Object.values(routes).map(value => value.provider));
    if (thread.modelProvider !== 'openai' && !managedProviders.has(thread.modelProvider)) {
      states.set(id, {key, passthrough: true}); return send(false);
    }
    if (!route && thread.modelProvider === 'openai') { states.set(id, {key}); return send(); }
    if (thread.ephemeral) throw new Error('Switch models in a saved chat.');
    if (thread.status?.type === 'active') throw new Error('Wait for the current reply to finish before switching models.');
    let current;
    try {
      current = await client.enqueueRequest('thread/resume', {threadId: id, excludeTurns: true}, options);
    } catch (error) {
      // Native thread/start already selected this provider and its context, but
      // a fresh loaded session has no resumable rollout until the first turn.
      // Keep that session alive; all other resume errors remain visible.
      if (route && thread.modelProvider === target && thread.preview === '' &&
          error?.message?.includes('no rollout found for thread id ' + id)) {
        states.set(id, {key});
        return send();
      }
      throw error;
    }
    const retained = {cwd: current.cwd, approvalPolicy: current.approvalPolicy,
                      approvalsReviewer: current.approvalsReviewer, runtimeWorkspaceRoots: current.runtimeWorkspaceRoots};
    if (current.activePermissionProfile?.id) retained.permissions = current.activePermissionProfile.id;
    else {
      const modes = {readOnly: 'read-only', workspaceWrite: 'workspace-write', dangerFullAccess: 'danger-full-access'};
      retained.sandbox = modes[current.sandbox?.type];
      if (!retained.sandbox) throw new Error('This chat uses permissions that cannot be safely restored during a model switch.');
    }
    let config;
    if (route) {
      // Refresh old saved 128K defaults from the current per-model preference.
      config = {web_search: 'disabled', model_context_window: route.context_window,
                model_auto_compact_token_limit: route.compact_limit};
    } else {
      const global = (await client.sendConfigReadRequest({includeLayers: false}, options)).config ?? {};
      const requested = global.model_context_window;
      const window = Number.isSafeInteger(requested) && requested > 0
        ? Math.min(requested, nativeInfo.maximum) : nativeInfo.window;
      const compact = Math.floor(window * .9);
      config = {model_context_window: window,
                model_auto_compact_token_limit: Number.isSafeInteger(global.model_auto_compact_token_limit)
                  ? Math.min(global.model_auto_compact_token_limit, compact) : compact,
                web_search: global.web_search ?? 'cached'};
    }
    if (!retained.permissions && current.sandbox?.type === 'workspaceWrite') {
      config.sandbox_workspace_write = {writable_roots: current.sandbox.writableRoots,
        network_access: current.sandbox.networkAccess, exclude_slash_tmp: current.sandbox.excludeSlashTmp,
        exclude_tmpdir_env_var: current.sandbox.excludeTmpdirEnvVar};
    }
    await client.enqueueRequest('thread/unsubscribe', {threadId: id}, options);
    try {
      const resumed = await client.enqueueRequest('thread/resume',
        {...retained, threadId: id, model, modelProvider: target, config, excludeTurns: true}, options);
      if (resumed.thread?.id !== id || resumed.modelProvider !== target || resumed.model !== model)
        throw new Error('The chat did not switch to the selected model. Retry before sending.');
      if (JSON.stringify(resumed.sandbox) !== JSON.stringify(current.sandbox) ||
          JSON.stringify(resumed.approvalPolicy) !== JSON.stringify(current.approvalPolicy) ||
          resumed.approvalsReviewer !== current.approvalsReviewer || resumed.cwd !== current.cwd)
        throw new Error('Chat permissions changed while switching. No message was sent.');
    } catch (error) {
      try { await client.enqueueRequest('thread/unsubscribe', {threadId: id}, options);
        await client.enqueueRequest('thread/resume',
        {...retained, threadId: id, model: current.model, modelProvider: current.modelProvider, excludeTurns: true}, options); } catch {}
      throw error;
    }
    states.set(id, {key});
    return send();
  })();
  states.set(id, {pending});
  return pending.catch(error => { states.delete(id); throw error; });
}
