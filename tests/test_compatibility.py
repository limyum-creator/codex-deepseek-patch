from windows_support import background_options
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import compatibility as c
from tests.test_patch import file_entry, write_asar

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    guard = c.GUARD
    send = 'async sendRequest(e,t,n){' + guard + 'return e===`config/read`?this.sendConfigReadRequest(t,n):this.enqueueRequest(e,t,e===`plugin/list`&&n?.timeoutMs==null?{...n,timeoutMs:O1t}:n)}'
    prewarm = ('async prewarmThreadStart(e,t){' + guard + 'let n=t?.priority??`critical`,r=u1t(`thread/start`,t?.source),i=t?.trace===void 0?wde({attributes:{"app_server.method":`thread/start`,"app_server.priority":n,"app_server.source":r},childName:`thread.prewarm`,rootName:`desktop.thread_prewarm`}):null;try{let a=await this.enqueueRequest(`thread/start`,e,{...t,priority:n,trace:t?.trace??i?.trace??null},e=>{this.dispatchMessage?.(`thread-prewarm-start`,{request:e,savedEnvironmentConfig:t?.savedEnvironmentConfig,hostId:this.hostId,retainResponse:t?.onOutcomeUnknown!=null||void 0,priority:n,source:r,timeoutMs:t?.timeoutMs,expiresAtMs:t?.timeoutMs!=null&&t.timeoutMs>0?Date.now()+t.timeoutMs:void 0,...e.trace==null?{}:{dispatchedAtMs:Date.now()}})});return i?.finish(),a}catch(e){throw i?.finish(e),e}}')
    return ('const untouched=`literal ${1+2}`;class Client{' + send + prewarm + 'consume(f){return f.canUsePrewarmedThread&&this.params.prewarmedThreadManager.hasPrewarmedThread()&&this.params.prewarmedThreadManager.consumePrewarmedThread()}}')


class CompatibilityTests(unittest.TestCase):
    def test_exact_insertions_preserve_all_other_source(self):
        source = fixture()
        route = (ROOT / 'route.js').read_text()
        patched, evidence = c.apply_source(source, route)
        reconstructed = patched[:-len('\n;\n' + route + '\n')]
        shift = 0
        for edit in evidence['edits']:
            start = edit['start'] + shift
            self.assertEqual(reconstructed[start:start + len(edit['after'])], edit['after'])
            reconstructed = reconstructed[:start] + reconstructed[start + len(edit['after']):]
        self.assertEqual(reconstructed, source)
        self.assertEqual(evidence['status'], 'compatible')
        self.assertEqual(len(evidence['edits']), 3)
        self.assertTrue(patched.endswith(route + '\n'))
        with self.assertRaises(c.CompatibilityError):
            c.analyze_source(patched)

    def test_minified_identifier_renames(self):
        source = fixture()
        # Rename only identifier tokens, preserving property spellings/strings.
        tokens = list(c._tokens(source))
        for token, start, end in reversed(tokens):
            if token in {'e', 't', 'n', 'r', 'i', 'a', 'f', 'O1t', 'u1t', 'wde'}:
                source = source[:start] + '_new_' + token + source[end:]
        result = c.analyze_source(source)
        self.assertEqual(result['request']['p'], '_new_t')
        self.assertEqual(result['prewarm']['consume_plan'], '_new_f')
        self.run_probe(source)

    def test_altered_control_flow_fails_closed(self):
        source = fixture()
        for old, new in [
            ('return e===`config/read`?', 'return e!==`config/read`?'),
            ('this.enqueueRequest(e,t,', 'this.enqueueRequest(e,{...t},'),
            ('this.enqueueRequest(`thread/start`,e,', 'this.enqueueRequest(`thread/resume`,e,'),
            ('let n=t?.priority??`critical`', 'e={};let n=t?.priority??`critical`'),
            ('try{let a=await this.enqueueRequest', 'try{if(e) return;let a=await this.enqueueRequest'),
            ('hostId:this.hostId', 'hostId:`local`'),
            ('return i?.finish(),a', 'return e'),
            ('savedEnvironmentConfig:t?.savedEnvironmentConfig', 'savedEnvironmentConfig:null'),
        ]:
            with self.subTest(old=old), self.assertRaises(c.CompatibilityError):
                c.analyze_source(source.replace(old, new, 1))

    def test_missing_ambiguous_and_marked_sources_refused(self):
        source = fixture()
        methods = c.analyze_source(source)['methods']
        for changed in [source + methods['sendRequest']['source'], source + methods['prewarmThreadStart']['source'],
                        source + 'f.canUsePrewarmedThread&&this.params.prewarmedThreadManager.hasPrewarmedThread()',
                        source + '.consumePrewarmedThread()', source + c.MARKER, source + c.HELPER,
                        source.replace('sendRequest(', 'sendOther(', 1),
                        source.replace('class Client{', 'class Client{sendRequest(){return null}'), 'unrelated']:
            with self.subTest(changed=changed[-80:]), self.assertRaises(c.CompatibilityError):
                c.analyze_source(changed)

    def test_quoted_or_commented_methods_not_anchors(self):
        source = fixture()
        for wrapped in ['/*' + source + '*/', "'" + source + "'", '`' + source.replace('`', '\\`') + '`']:
            with self.assertRaises(c.CompatibilityError):
                c.analyze_source(wrapped)
        # An unrelated client with same public method name is accepted.
        c.analyze_source(source + 'class Other{async sendRequest(a){return a}}')

    def test_changed_hash_filename_discovered_and_integrity_checked(self):
        source = fixture().encode()
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'app.asar'
            write_asar(archive, {'webview': {'files': {'assets': {'files': {'app-initial-newHASH999.js': file_entry(source)}}}}}, source)
            name, text, result = c.locate_bundle(archive)
            self.assertEqual(name, 'webview/assets/app-initial-newHASH999.js')
            self.assertEqual(text.encode(), source)
            self.assertEqual(result['status'], 'compatible')
            # Two compatible bundles are ambiguous and cannot be chosen by hash.
            members = {'app-initial-a.js': file_entry(source), 'app-initial-b.js': file_entry(source)}
            write_asar(archive, {'webview': {'files': {'assets': {'files': members}}}}, source)
            with self.assertRaises(c.CompatibilityError):
                c.locate_bundle(archive)

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for behavior contract')
    def test_node_probe_exercises_request_and_prewarm_contracts(self):
        self.run_probe(fixture())

    @unittest.skipUnless(shutil.which('node'), 'Node.js required for behavior contract')
    def test_behavior_probe_rejects_changed_route_semantics(self):
        route = (ROOT / 'route.js').read_text().replace('"provider":"deepseek-desktop"', '"provider":"wrong-provider"')
        _, evidence = c.apply_source(fixture(), route)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'evidence.json'
            path.write_text(json.dumps(evidence))
            result = subprocess.run(['node', ROOT / 'scripts/check_compatibility.cjs', path], capture_output=True, text=True, **background_options())
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Compatibility behavior probe failed', result.stderr)

    def run_probe(self, source):
        if not shutil.which('node'):
            self.skipTest('Node.js unavailable')
        patched, evidence = c.apply_source(source, (ROOT / 'route.js').read_text())
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'evidence.json'
            path.write_text(json.dumps(evidence))
            result = subprocess.run(['node', ROOT / 'scripts/check_compatibility.cjs', path], capture_output=True, text=True, **background_options())
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreater(json.loads(result.stdout)['assertions'], 200)
            js = Path(temp) / 'candidate.mjs'
            js.write_text(patched)
            syntax = subprocess.run(['node', '--check', js], capture_output=True, text=True, **background_options())
            self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == '__main__':
    unittest.main()
