'use strict';
// Test-only bridge: execute the exact patched request method against a real RPC client.
const fs = require('node:fs'), vm = require('node:vm'), readline = require('node:readline');
const evidence = JSON.parse(fs.readFileSync(process.argv[2], 'utf8')).evidence;
const ctx = vm.createContext({[evidence.request.timeout]: 271828});
vm.runInContext(evidence.route_js, ctx);
const method = vm.runInContext('({' + evidence.probe.send_request + '}).sendRequest', ctx);
let counter = 0;
const pending = new Map();
const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const rpc = (method, params) => new Promise((resolve, reject) => {
  const id = ++counter; pending.set(id, {resolve, reject}); emit({rpc: id, method, params});
});
const client = {hostId: 'local', dispatchMessage() {},
  enqueueRequest: rpc, sendConfigReadRequest: params => rpc('config/read', params)};
readline.createInterface({input: process.stdin}).on('line', line => {
  const value = JSON.parse(line);
  if (value.rpc) {
    const item = pending.get(value.rpc); pending.delete(value.rpc);
    if (value.error) item.reject(new Error(value.error)); else item.resolve(value.result);
  } else {
    method.call(client, value.method, value.params, {}).then(
      result => emit({done: true, result}), error => emit({done: true, error: error.message}));
  }
});
