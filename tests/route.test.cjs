const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../route.js'), 'utf8'), context);
const route = context.codexPatchAppServerParams;
for (const provider of [undefined, null, 'openai']) {
  const params = {model: 'deepseek-flash', modelProvider: provider, serviceTier: 'priority',
                  config: {keep: 42}, dynamicTools: [{name: 'example'}]};
  const result = route('thread/start', params, 'local');
  assert.equal(result.modelProvider, 'deepseek-desktop');
  assert.equal(result.serviceTier, null);
  assert.equal(result.config.keep, 42);
  assert.equal(result.config.web_search, 'disabled');
  assert.equal(result.dynamicTools, params.dynamicTools);
  assert.equal(params.modelProvider, provider);
  assert.equal(params.config.web_search, undefined);
}
for (const [method, params, host] of [
  ['thread/start', {model: 'gpt-6.1-sol'}, 'local'],
  ['thread/start', {model: 'deepseek-v4-pro'}, 'local'],
  ['thread/start', {model: 'deepseek-flash'}, 'remote'],
  ['thread/start', {model: 'deepseek-flash'}, 'durable'],
  ['thread/start', {model: 'deepseek-flash', modelProvider: 'custom'}, 'local'],
  ['thread/resume', {model: 'deepseek-flash'}, 'local'],
  ['turn/start', {model: 'deepseek-flash'}, 'local'],
  ['thread/list', {modelProviders: ['custom']}, 'local'],
  ['thread/start', null, 'local'],
]) assert.equal(route(method, params, host), params);
assert.equal(route('thread/list', {}, 'local').modelProviders.length, 0);
for (const model of ['grok-4.5', 'grok-4.7', 'gemini-3.8-flash-high', 'claude-opus-4-6-thinking']) {
  for (const modelProvider of [undefined, null, 'openai', 'easycli-desktop']) {
    const params = {model, modelProvider, config: {keep: 42}, serviceTier: 'priority'};
    const result = route('thread/start', params, 'local');
    assert.equal(result.modelProvider, 'easycli-desktop');
    assert.equal(result.model, model);
    assert.equal(result.serviceTier, null);
    assert.equal(result.config.keep, 42);
    assert.equal(result.config.web_search, 'disabled');
    assert.equal(params.modelProvider, modelProvider);
    for (const method of ['thread/resume', 'turn/start']) assert.equal(route(method, params, 'local'), params);
    assert.equal(route('thread/start', params, 'remote'), params);
  }
  const explicit = {model, modelProvider: 'custom'};
  assert.equal(route('thread/start', explicit, 'local'), explicit);
  const window = {'grok-4.5':500000, 'grok-4.7':500000, 'gemini-3.8-flash-high':1048576, 'claude-opus-4-6-thinking':1000000}[model];
  const defaults = route('thread/start', {model}, 'local').config;
  assert.equal(defaults.model_context_window, window);
  assert.equal(defaults.model_auto_compact_token_limit, Math.floor(window * 0.9));
  const reduced = {model, config:{model_context_window:200000,model_auto_compact_token_limit:160000}};
  const adjusted = route('thread/start', reduced, 'local');
  assert.equal(adjusted.config.model_context_window, 200000);
  assert.equal(adjusted.config.model_auto_compact_token_limit, 160000);
  assert.equal(reduced.config.model_context_window, 200000);
  const tooLarge = route('thread/start', {model, config:{model_context_window:9000000,model_auto_compact_token_limit:8000000}}, 'local');
  assert.equal(tooLarge.config.model_context_window, window);
  assert.equal(tooLarge.config.model_auto_compact_token_limit, Math.floor(window * 0.9));
}
for (const model of ['__proto__', 'constructor', 'gemini-4', 'claude-opus-5-5']) {
  const params = {model};
  assert.equal(route('thread/start', params, 'local'), params);
}

async function actualBundleTest(filename) {
  const source = fs.readFileSync(filename, 'utf8');
  const start = source.indexOf('async sendRequest(e,t,n){if(this.dispatchMessage==null)');
  const end = source.indexOf('async sendAppServerExtensionRequest(', start);
  assert(start > 0 && end > start);
  vm.runInContext('class Client {' + source.slice(start, end) + '}; globalThis.Client=Client', context);
  for (const hostId of ['local', 'remote']) {
    const client = new context.Client();
    client.hostId = hostId; client.dispatchMessage = () => {};
    client.enqueueRequest = async (method, params) => ({method, params});
    const result = await client.sendRequest('thread/start', {model: 'deepseek-flash'}, {});
    assert.equal(result.params.modelProvider, hostId === 'local' ? 'deepseek-desktop' : undefined);
  }
  assert(source.includes('this.params.hostId!==`local`&&f.canUsePrewarmedThread&&'));
  assert(source.includes('e=codexPatchAppServerParams(`thread/start`,e,this.hostId);'));
}
(async () => {
  if (process.argv[2]) await actualBundleTest(process.argv[2]);
  console.log('PASS: local Flash routing, GPT/remote/existing/custom provider preservation' +
              (process.argv[2] ? ', actual patched request method' : ''));
})().catch(error => {console.error(error); process.exitCode = 1;});

async function managedRoutingTests() {
  if (!context.codexPatchManagedRequest) return;
  const firstCalls=[];
  const fresh={hostId:'local',enqueueRequest:async(method,params)=>{
    firstCalls.push(method);
    if(method==='thread/read')return {thread:{id:'new',modelProvider:'easycli-desktop',preview:'',turns:[],ephemeral:false,status:{type:'idle'}}};
    if(method==='thread/resume')throw new Error('no rollout found for thread id new');
    assert.equal(method,'turn/start','An empty chat must not be unloaded');
    return {sent:true};
  }};
  await context.codexPatchManagedRequest(fresh,'turn/start',{threadId:'new',model:'grok-4.7'},{});
  assert.deepEqual(firstCalls,['thread/read','thread/resume','turn/start']);
  for (const [preview,message] of [['has a user message','no rollout found for thread id new'],['','permission denied'],['','no rollout found for thread id another']]) {
    const guarded={hostId:'local',enqueueRequest:async(method)=>{
      if(method==='thread/read')return {thread:{id:'new',modelProvider:'easycli-desktop',preview,ephemeral:false,status:{type:'idle'}}};
      if(method==='thread/resume')throw new Error(message);
      assert.fail('An unrelated resume error must not send a message');
    }};
    await assert.rejects(context.codexPatchManagedRequest(guarded,'turn/start',{threadId:'new',model:'grok-4.7'},{}));
  }
  let current = {id:'saved', model:'grok-4.5', modelProvider:'easycli-desktop', ephemeral:false, status:{type:'idle'}};
  const calls = [];
  const client = {hostId:'local',
    async enqueueRequest(method, params) {
      calls.push({method,params});
      if (method === 'thread/read') return {thread:{...current}};
      if (method === 'thread/unsubscribe') return {status:'unsubscribed'};
      if (method === 'thread/resume') {
        current = {...current, model:params.model ?? current.model, modelProvider:params.modelProvider ?? current.modelProvider};
        return {thread:{...current},model:current.model,modelProvider:current.modelProvider,
          approvalPolicy:'never',sandbox:{type:'readOnly'},activePermissionProfile:{id:':read-only'},cwd:'/safe/work'};
      }
      return {sent:true};
    }};
  await context.codexPatchManagedRequest(client,'turn/start',{threadId:'saved',model:'grok-4.7'}, {});
  assert.deepEqual(calls.map(x=>x.method),['thread/read','thread/resume','thread/unsubscribe','thread/resume','turn/start']);
  assert.equal(calls[3].params.config.model_context_window,500000);
  assert.equal(calls[3].params.permissions,':read-only');
  assert.equal(calls[3].params.approvalPolicy,'never');
  assert.equal(calls[3].params.cwd,'/safe/work');
  calls.length=0;
  await context.codexPatchManagedRequest(client,'turn/start',{threadId:'saved',model:'grok-4.7'}, {});
  assert.deepEqual(calls.map(x=>x.method),['turn/start']);
  const active = {hostId:'local',enqueueRequest:async method => {
    if (method==='thread/read') return {thread:{...current,status:{type:'active'}}};
    assert.fail('An active turn must not be unloaded');
  }};
  await assert.rejects(context.codexPatchManagedRequest(active,'turn/start',{threadId:'saved',model:'grok-4.5'},{}),/finish/);
  const outsider = {hostId:'local',enqueueRequest:async (method,params) => {
    if (method==='thread/read') return {thread:{...current,modelProvider:'outside'}};
    assert.equal(method,'turn/start');assert.equal(params.serviceTier,'priority');return {sent:true};
  }};
  for (let i=0;i<2;i++)
    await context.codexPatchManagedRequest(outsider,'turn/start',{threadId:'saved',model:'grok-4.7',serviceTier:'priority'},{});
  const rollback=[];
  const broken={hostId:'local',enqueueRequest:async(method,params)=>{
    rollback.push({method,params});
    if(method==='thread/read')return {thread:{...current,model:'grok-4.5'}};
    if(method==='thread/unsubscribe')return {};
    if(method==='thread/resume'){
      if(params.model==='grok-4.7')throw new Error('provider unavailable');
      return {thread:{...current},model:'grok-4.5',modelProvider:'easycli-desktop',sandbox:{type:'readOnly'},cwd:'/safe/work'};
    }
    assert.fail('Failed switching must not send the turn');
  }};
  await assert.rejects(context.codexPatchManagedRequest(broken,'turn/start',{threadId:'saved',model:'grok-4.7'},{}),/unavailable/);
  assert.equal(rollback.at(-2).method,'thread/unsubscribe');
  assert.equal(rollback.at(-1).params.model,'grok-4.5');
  console.log('PASS: cached model switches, busy-chat guard and explicit-provider preservation');
}
managedRoutingTests().catch(error=>{console.error(error);process.exitCode=1;});
