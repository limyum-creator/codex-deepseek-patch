from windows_support import background_options
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch as mock
import windows_update_ui as update
from compatibility import CompatibilityError
from test_patch import file_entry, write_asar
import patch


def fixture(updater='updater', common='common', offers='offers', interval='interval', store='store'):
    # Synthetic contract: no shipped application implementation is included.
    return (f'class Owner{{async initializeWindowsUpdater(){{let {store}=flavor.i.getWindowsStoreConfig();'
            f'let {offers}=makeOffers(),{interval}=getInterval(),{common}={{nativeAddon:addon,offers:{offers},allowed:()=>true}},'
            f'{updater}=makeNative();if(await {updater}.initialize(),!{updater}.hasUpdater())'
            f'{{this.lastUnavailableReason={updater}.getUnavailableReason();return}}'
            f'this.updater={updater}}}}}')


class ContractTests(unittest.TestCase):
    def test_exact_span_and_names(self):
        for names in [(), ('u$','c$','o$','i$','s$')]:
            source=fixture(*names);result,evidence=update.apply_source(source)
            self.assertEqual(source[evidence['begin']:evidence['end']],evidence['old'])
            self.assertEqual(result,evidence['helper']+'\n'+source[:evidence['begin']]+evidence['new']+source[evidence['end']:])
            self.assertIn('process.platform===`win32`',result)
            self.assertIn('missing current windows package family',result)
            with self.assertRaises(CompatibilityError):update.apply_source(result)

    def test_unknown_or_duplicate_contract_is_rejected(self):
        for source in ['unrelated',fixture()+fixture(),fixture().replace('getUnavailableReason()','somethingElse()'),
                       fixture().replace('offers:offers','unknown:offers')]:
            with self.subTest(source=source), self.assertRaises(CompatibilityError):update.apply_source(source)

    def test_archive_identity_and_member_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            archive=Path(temp)/'app.asar';data=fixture().encode()
            tree={'.vite':{'files':{'build':{'files':{'bootstrap-new.js':file_entry(data)}}}}}
            write_asar(archive,tree,data)
            name,result,evidence=update.locate_bundle(archive)
            self.assertEqual(name,'.vite/build/bootstrap-new.js')
            self.assertIn(update.MARKER,result)
            write_asar(archive,tree,data[:-1]+b'X')
            with self.assertRaises(patch.PatchError):update.locate_bundle(archive)

    def test_archive_comparison_accepts_only_declared_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            a,b=Path(temp)/'a',Path(temp)/'b';data=b'original'
            write_asar(a,{'a.js':file_entry(data),'b.js':file_entry(data),'untouched':file_entry(data)},data)
            patch.repack(a,b,{'a.js':b'one','b.js':b'two'})
            self.assertEqual(patch.compare_archives(a,b,['a.js','b.js']),3)
            with self.assertRaises(patch.PatchError):patch.compare_archives(a,b,'a.js')
            with self.assertRaises(patch.PatchError):patch.compare_archives(a,b,['a.js','b.js','untouched'])

    def test_actual_initializer_preserves_nonmatching_platform_and_errors(self):
        import shutil,subprocess
        if not shutil.which('node'):self.skipTest('Node.js unavailable')
        source,evidence=update.apply_source(fixture())
        # Exercise the edited original method, with the adapter stubbed at its boundary.
        body=source[len(evidence['helper'])+1:]
        script='''const assert=require('node:assert/strict');
        (async()=>{for(const [platform,reason,expected] of [['win32','missing current windows package family',true],['win32','disabled by policy',false],['darwin','missing current windows package family',false]]){
          let calls=0,nativeCalls=0;
          const process={platform},flavor={i:{getWindowsStoreConfig:()=>({storeProductId:'product'})}},addon={};
          const makeOffers=()=>({}),getInterval=()=>100;
          const makeNative=()=>({initialize:async()=>{nativeCalls++},hasUpdater:()=>false,getUnavailableReason:()=>reason});
          class CodexPatchManagedWindowsUpdater{constructor(o){assert.equal(o.checkIntervalMs,100);calls++}async initialize(){}hasUpdater(){return true}}
          BODY
          const owner=new Owner();await owner.initializeWindowsUpdater();
          assert.equal(calls,Number(expected));assert.equal(nativeCalls,1);
          assert.equal(!!owner.updater,expected);
        }})().catch(e=>{console.error(e);process.exit(1)});'''.replace('BODY',body)
        r=subprocess.run(['node','-e',script],capture_output=True,text=True,**background_options())
        self.assertEqual(r.returncode,0,r.stderr)
