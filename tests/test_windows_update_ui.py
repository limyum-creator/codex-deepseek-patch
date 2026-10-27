from windows_support import background_options
"""Execute the managed updater against a fake Electron host and official offers."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "windows_update_ui.js"
PRODUCT_ID = "9PLM9XGG6VKS"


NODE_HARNESS = r"""
const fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const dialogs=[],errors=[],opened=[],notifications=[],lifecycle=[],requests=[];
let allowed=input.allowed!==false,offer=input.offer,releaseGate;
let gate=input.coalesce?new Promise(resolve=>{releaseGate=resolve}):null;
const listeners=[];
class Notification {
  static isSupported(){return true}
  constructor(options){this.options=options;this.listeners={}}
  on(name,fn){this.listeners[name]=fn;listeners.push({name,fn});return this}
  once(name,fn){return this.on(name,fn)}
  show(){notifications.push(this.options)}
  close(){}
}
const electron={
  app:{isPackaged:true,getLocale:()=>input.locale||'en-US',getName:()=>'Codex',getVersion:()=>'1.0.0.0'},
  dialog:{
    showMessageBox:async options=>{dialogs.push(options);return {response:input.response??1}},
    showErrorBox:(title,content)=>{errors.push({title,content})}
  },
  shell:{openExternal:async url=>{opened.push(url);if(input.failOpen)throw Error('fixture-store-open-failed')}},
  Notification
};
const forbiddenNative=new Proxy({}, {get(){throw Error('Managed detection must not access nativeAddon')}});
const options={
  offers:{
    options:{storeReleaseFeedUrl:'https://persistent.oaistatic.com/codex-app-prod/windows-release.json',buildVersion:'1.0.0.0'},
    direct:async source=>{requests.push(source);if(gate)await gate;if(input.offerError)throw Error(input.offerError);return offer}
  },
  allowed:()=>allowed,checkIntervalMs:0,checkForUpdatesOnInitialize:false,
  storeProductId:'9PLM9XGG6VKS',nativeAddon:forbiddenNative,
  onUpdateLifecycleStateChanged:state=>lifecycle.push(state),
  onUpdateReadyChanged:ready=>{if(ready)throw Error('An offer is not a downloaded update')}
};
const context={console,process:{platform:'win32',arch:'x64'},URL,Promise,
  require:name=>{if(name==='electron')return electron;throw Error('Unexpected require: '+name)},
  setTimeout,clearTimeout,setInterval,clearInterval};
const Updater=vm.runInNewContext(fs.readFileSync(input.helperPath,'utf8')+'\n;CodexPatchManagedWindowsUpdater',context,{timeout:1000});
(async()=>{
  const updater=new Updater(options,electron);
  await updater.initialize();
  const initialized={hasUpdater:updater.hasUpdater(),unavailableReason:updater.getUnavailableReason(),ready:updater.getIsUpdateReady()};
  let info=null,secondInfo=null;
  switch(input.action){
    case 'information':info=await updater.checkForUpdateInformation();break;
    case 'manual':await updater.checkForUpdates();break;
    case 'install':await updater.installUpdatesIfAvailable();break;
    case 'background':
      await updater.checkForUpdatesInBackground();await updater.checkForUpdatesInBackground();
      if(input.nextOffer){offer=input.nextOffer;await updater.checkForUpdatesInBackground()}
      break;
    case 'coalesce':{
      const a=updater.checkForUpdateInformation(),b=updater.checkForUpdateInformation();
      await Promise.resolve();await Promise.resolve();releaseGate();
      [info,secondInfo]=await Promise.all([a,b]);break;
    }
    case 'deny-during-manual':{
      const pending=updater.checkForUpdates();
      await Promise.resolve();allowed=false;releaseGate();await pending;break;
    }
    case 'recheck':
      info=await updater.checkForUpdateInformation();offer=input.nextOffer;
      secondInfo=await updater.checkForUpdateInformation();break;
  }
  process.stdout.write(JSON.stringify({initialized,info,secondInfo,dialogs,errors,opened,notifications,lifecycle,requests,ready:updater.getIsUpdateReady()}));
})().catch(error=>{console.error(error.stack);process.exitCode=1});
"""


def available(version="2.0.0.0"):
    return {"source": "store-unavailable", "buildVersion": version,
            "packageInfo": {"packageUrl": "https://persistent.oaistatic.com/synthetic.msix",
                            "length": None, "sha256": None}}


@unittest.skipUnless(shutil.which("node"), "Node is required to execute the production helper")
class ManagedWindowsUpdateBehaviorTests(unittest.TestCase):
    def run_case(self, action="information", **overrides):
        self.assertTrue(HELPER.is_file(), "The actual injected helper must exist")
        payload = {"helperPath": str(HELPER), "action": action, "offer": available(), **overrides}
        result = subprocess.run([shutil.which("node"), "-e", NODE_HARNESS],
                                input=json.dumps(payload), capture_output=True,
                                text=True, encoding="utf-8", timeout=10, **background_options())
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_never_ready(self, data):
        self.assertEqual(data["initialized"], {"hasUpdater": True, "unavailableReason": None, "ready": False})
        self.assertFalse(data["ready"])
        self.assertNotIn("ready", data["lifecycle"])
        self.assertNotIn("downloading", data["lifecycle"])
        self.assertNotIn("installing", data["lifecycle"])

    def test_real_release_feed_offer_reports_available_without_claiming_downloaded(self):
        data = self.run_case()
        self.assertEqual(data["requests"], ["store-unavailable"])
        self.assertEqual(data["info"]["status"], "available")
        self.assertEqual(data["info"]["availableBuildVersion"], "2.0.0.0")
        self.assert_never_ready(data)

    def test_no_offer_reports_up_to_date(self):
        data = self.run_case(offer={"source": "none"})
        self.assertEqual(data["info"]["status"], "up_to_date")
        self.assertFalse(data["info"].get("availableBuildVersion"))
        self.assert_never_ready(data)

    def test_release_feed_failure_is_an_error_and_never_up_to_date(self):
        data = self.run_case(offerError="fixture-release-feed-unreachable")
        self.assertEqual(data["info"]["status"], "error")
        self.assert_never_ready(data)
        manual = self.run_case("manual", offerError="fixture-release-feed-unreachable")
        self.assertTrue(manual["dialogs"] or manual["errors"])
        text = json.dumps(manual["dialogs"] + manual["errors"]).lower()
        self.assertRegex(text, r"(unable|failed|error|unavailable|could not)")
        self.assertNotIn("is up to date", text)
        self.assertEqual(manual["opened"], [])

    def test_incomplete_feed_results_are_reported_as_errors(self):
        for offer in ({"source": "waiting"},
                      {"source": "store-unavailable", "buildVersion": "2.0.0.0"},
                      {**available(), "buildVersion": "unparseable"}):
            with self.subTest(offer=offer):
                data = self.run_case(offer=offer)
                self.assertEqual(data["info"]["status"], "error")
                self.assert_never_ready(data)

    def test_disallowed_checks_do_not_request_the_feed(self):
        data = self.run_case(allowed=False)
        self.assertEqual(data["requests"], [])
        self.assertEqual(data["info"]["status"], "unavailable")
        manual = self.run_case("manual", allowed=False)
        background = self.run_case("background", allowed=False)
        self.assertEqual(manual["requests"], [])
        self.assertEqual(background["requests"], [])
        self.assertEqual(background["notifications"], [])

    def test_concurrent_checks_share_the_same_release_feed_request(self):
        data = self.run_case("coalesce", coalesce=True)
        self.assertEqual(data["requests"], ["store-unavailable"])
        self.assertEqual(data["info"], data["secondInfo"])
        self.assertEqual(data["info"]["status"], "available")

    def test_policy_revoked_while_feed_is_pending_blocks_store_launch(self):
        data = self.run_case("deny-during-manual", coalesce=True, response=0)
        self.assertEqual(data["requests"], ["store-unavailable"])
        self.assertEqual(data["opened"], [])
        self.assertEqual(data["notifications"], [])
        self.assert_never_ready(data)

    def test_subsequent_check_refreshes_status_when_source_has_caught_up(self):
        data = self.run_case("recheck", nextOffer={"source": "none"})
        self.assertEqual(data["info"]["status"], "available")
        self.assertEqual(data["secondInfo"]["status"], "up_to_date")
        self.assertFalse(data["secondInfo"].get("availableBuildVersion"))

    def test_manual_offer_names_the_real_version_and_opens_the_product_store_page(self):
        data = self.run_case("manual", response=0)
        self.assertEqual(len(data["dialogs"]), 1)
        text = json.dumps(data["dialogs"][0], ensure_ascii=False)
        self.assertIn("2.0.0.0", text)
        self.assertRegex(text.lower(), r"(store|official update)")
        self.assertEqual(len(data["opened"]), 1)
        self.assertTrue(data["opened"][0].startswith("ms-windows-store:"))
        self.assertIn(PRODUCT_ID, data["opened"][0])
        self.assert_never_ready(data)

    def test_cancel_leaves_the_store_closed(self):
        data = self.run_case("manual", response=1)
        self.assertEqual(data["opened"], [])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["dialogs"][0]["cancelId"], 1)

    def test_install_rpc_uses_the_official_channel_and_never_native_installation(self):
        data = self.run_case("install", response=0)
        self.assertEqual(data["requests"], ["store-unavailable"])
        self.assertEqual(len(data["opened"]), 1)
        self.assertIn(PRODUCT_ID, data["opened"][0])
        self.assert_never_ready(data)

    def test_store_launch_failure_is_caught_and_shown(self):
        data = self.run_case("manual", response=0, failOpen=True)
        self.assertEqual(len(data["opened"]), 1)
        self.assertTrue(data["errors"] or len(data["dialogs"]) >= 2)
        error = data["errors"][-1] if data["errors"] else data["dialogs"][-1]
        text = json.dumps(error, ensure_ascii=False).lower()
        self.assertRegex(text, r"(store|official update)")
        self.assertRegex(text, r"(unable|failed|error|could not|cannot)")

    def test_background_notification_is_once_per_new_version_and_does_not_open_store(self):
        data = self.run_case("background", nextOffer=available("3.0.0.0"))
        self.assertEqual(len(data["notifications"]), 2)
        self.assertIn("2.0.0.0", json.dumps(data["notifications"][0]))
        self.assertIn("3.0.0.0", json.dumps(data["notifications"][1]))
        self.assertEqual(data["dialogs"], [])
        self.assertEqual(data["opened"], [])
        self.assert_never_ready(data)

    def test_background_no_offer_and_failure_do_not_notify(self):
        for overrides in ({"offer": {"source": "none"}}, {"offerError": "fixture-feed-error"}):
            with self.subTest(overrides=overrides):
                data = self.run_case("background", **overrides)
                self.assertEqual(data["notifications"], [])
                self.assertEqual(data["dialogs"], [])
                self.assertEqual(data["opened"], [])

    def test_manual_up_to_date_message_and_locale_are_accurate(self):
        english = self.run_case("manual", offer={"source": "none"})
        self.assertIn("up to date", json.dumps(english["dialogs"]).lower())
        chinese = self.run_case("manual", locale="zh-CN", response=1)
        self.assertRegex(json.dumps(chinese["dialogs"], ensure_ascii=False), r"[\u4e00-\u9fff]")
        self.assertIn("2.0.0.0", json.dumps(chinese["dialogs"]))


if __name__ == "__main__":
    unittest.main()
