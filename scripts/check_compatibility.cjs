#!/usr/bin/env node
'use strict';
// Execute only locally extracted narrow contracts, never a complete vendor app.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function main() {
  const evidence = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  assert.equal(evidence.format, 1);
  assert.equal(evidence.status, 'compatible');
  const probe = evidence.probe;
  assert.ok(probe && typeof evidence.route_js === 'string');
  const sandbox = Object.create(null);
  sandbox[evidence.request.timeout] = 271828;
  const context = vm.createContext(sandbox, {codeGeneration: {strings: false, wasm: false}});
  new vm.Script(evidence.route_js).runInContext(context, {timeout: 2000});
  const methods = new vm.Script(`({${probe.send_request},${probe.prewarm}})`).runInContext(context, {timeout: 2000});
  let assertions = 0;
  const equal = (a, b) => {assert.deepEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b))); assertions++;};
  const client = (hostId, dispatchMessage = () => {}) => ({
    hostId, dispatchMessage,
    enqueueRequest(method, params, options) { return {kind: 'enqueue', method, params, options}; },
    sendConfigReadRequest(params, options) { return {kind: 'config', params, options}; },
  });
  const defaults = {
    'deepseek-flash': {provider:'deepseek-desktop'},
    'grok-4.5': {provider:'easycli-desktop',context_window:500000,max_context_window:500000,compact_limit:450000},
    'grok-4.7': {provider:'easycli-desktop',context_window:500000,max_context_window:500000,compact_limit:450000},
    'gemini-3.8-flash-high': {provider:'easycli-desktop',context_window:1048576,max_context_window:1048576,compact_limit:943718},
    'claude-opus-4-6-thinking': {provider:'easycli-desktop',context_window:1000000,max_context_window:1000000,compact_limit:900000},
  };
  const routes = evidence.model_routes ?? Object.fromEntries(Object.keys(context.codexPatchModelRoutes()).map(model => [model, defaults[model]]));
  for (const hostId of ['local', 'remote-workpc']) {
    for (const model of [...new Set(['gpt-6.1-sol','deepseek-v4-pro',...Object.keys(routes)])]) {
      for (const provider of [undefined, null, 'openai', 'other-provider', 'deepseek-desktop', 'easycli-desktop']) {
        const params = {model, serviceTier: 'priority', config: {keep: 'yes', web_search: 'enabled'}};
        if (provider !== undefined) params.modelProvider = provider;
        const snapshot = structuredClone(params);
        const options = {priority: 'critical', timeoutMs: 12};
        const target = routes[model]?.provider;
        const expected = hostId === 'local' && target && (provider == null || provider === 'openai' || routes[model].context_window && provider === target)
          ? {...params, modelProvider: target, serviceTier: null, config: {...params.config, web_search: 'disabled'}} : params;
        if (expected !== params && routes[model]?.context_window) {
          const window = routes[model].context_window;
          expected.config.model_context_window = window;
          expected.config.model_auto_compact_token_limit = routes[model].compact_limit;
        }
        const c = client(hostId);
        const actual = await methods.sendRequest.call(c, 'thread/start', params, options);
        equal(actual, {kind: 'enqueue', method: 'thread/start', params: expected, options});
        equal(await methods.prewarmThreadStart.call(c, params, options), expected);
        equal(params, snapshot);
        if (expected === params) {assert.equal(actual.params, params); assertions++;}
        assert.equal(actual.options, options); assertions++;
      }
    }
  }
  for (const hostId of ['local', 'remote-workpc']) {
    const c = client(hostId);
    for (const params of [null, undefined, 'text', 4]) {
      assert.equal((await methods.sendRequest.call(c, 'thread/start', params)).params, params); assertions++;
      assert.equal(await methods.prewarmThreadStart.call(c, params), params); assertions++;
    }
    for (const method of ['thread/resume', 'turn/start', 'config/read', 'unrelated']) {
      const params = {model: 'deepseek-flash'};
      const options = {timeoutMs: 7};
      const actual = await methods.sendRequest.call(c, method, params, options);
      assert.equal(actual.params, params); assertions++;
      assert.equal(actual.options, options); assertions++;
      assert.equal(actual.kind, method === 'config/read' ? 'config' : 'enqueue'); assertions++;
    }
    for (const params of [{}, {modelProviders: null}, {modelProviders: ['openai']}]) {
      const actual = await methods.sendRequest.call(c, 'thread/list', params);
      equal(actual.params, hostId === 'local' && params.modelProviders == null ? {...params, modelProviders: []} : params);
    }
    for (const options of [undefined, {}, {timeoutMs: null}, {timeoutMs: 0}, {timeoutMs: 123}]) {
      const params = {keep: true};
      const actual = await methods.sendRequest.call(c, 'plugin/list', params, options);
      equal(actual.options, options?.timeoutMs == null ? {...options, timeoutMs: 271828} : options);
      assert.equal(actual.params, params); assertions++;
    }
    for (const method of ['thread/start', 'config/read']) {
      await assert.rejects(() => methods.sendRequest.call(client(hostId, null), method, {}), /missing a message dispatcher/); assertions++;
    }
    await assert.rejects(() => methods.prewarmThreadStart.call(client(hostId, null), {}), /missing a message dispatcher/); assertions++;
  }
  // Exercise the exact inserted local-host guard with the extracted plan name.
  const planName = evidence.prewarm.consume_plan;
  assert.match(planName, /^[A-Za-z_$][\w$]*$/);
  const consume = new vm.Script(`(function(${planName}) {return ${probe.consume_guard})})`).runInContext(context, {timeout: 2000});
  for (const hostId of ['local', 'remote-workpc']) {
    for (const canUsePrewarmedThread of [true, false]) {
      let calls = 0;
      const result = consume.call({params: {hostId, prewarmedThreadManager: {hasPrewarmedThread() {calls++; return true;}}}}, {canUsePrewarmedThread});
      assert.equal(result, hostId !== 'local' && canUsePrewarmedThread); assertions++;
      assert.equal(calls, hostId !== 'local' && canUsePrewarmedThread ? 1 : 0); assertions++;
    }
  }
  console.log(JSON.stringify({status: 'passed', assertions, source_sha256: evidence.source_sha256, patched_sha256: evidence.patched_sha256}));
}
main().catch(error => {console.error(`Compatibility behavior probe failed: ${error.message}`); process.exitCode = 1;});
