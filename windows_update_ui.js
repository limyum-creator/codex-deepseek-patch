// CodexPatchManagedWindowsUpdates: official release checks for a managed copy.
// Available releases are never reported as downloaded/prepared updates.
class CodexPatchManagedWindowsUpdater {
  constructor(options, electron = require('electron')) {
    this.options = options;
    this.electron = electron;
    this.inFlight = null;
    this.notified = new Set();
  }
  async initialize() {
    if (this.options.checkIntervalMs > 0) {
      this.timer = setInterval(() => this.checkForUpdatesInBackground(), this.options.checkIntervalMs);
      this.timer.unref();
      void this.checkForUpdatesInBackground();
    }
  }
  hasUpdater() { return true; }
  getUnavailableReason() { return null; }
  getIsUpdateReady() { return false; }
  text(zh, en) { return this.electron.app.getLocale().toLowerCase().startsWith('zh') ? zh : en; }
  async check() {
    if (!this.options.allowed()) return {status: 'unavailable', reason: 'Updates are disabled by policy.'};
    if (this.inFlight) return this.inFlight;
    let deadline;
    const work = (async () => {
      this.options.onUpdateLifecycleStateChanged?.('checking');
      try {
        const offers = this.options.offers;
        const offer = await Promise.race([
          offers.direct(offers.options.storeReleaseFeedUrl ? 'store-unavailable' : 'direct'),
          new Promise((_, reject) => { deadline = setTimeout(() => reject(Error('Official update check timed out.')), 25000); })
        ]);
        if (!this.options.allowed()) return {status: 'unavailable', reason: 'Updates are disabled by policy.'};
        if (offer.source === 'none') return {status: 'up_to_date'};
        if (!offer.packageInfo || !/^\d+(?:\.\d+)*$/.test(offer.buildVersion)) {
          throw Error('The official release feed returned an incomplete update.');
        }
        return {status: 'available', availableVersion: null, availableBuildVersion: offer.buildVersion};
      } catch (error) {
        return {status: 'error', reason: String(error?.message || error)};
      } finally { clearTimeout(deadline); this.options.onUpdateLifecycleStateChanged?.('idle'); }
    })().then(result => {
      this.options.logger?.info('Managed Windows official release check', {
        safe: {status: result.status, availableBuildVersion: result.availableBuildVersion ?? null}, sensitive: {}});
      return result;
    });
    this.inFlight = work;
    try { return await work; } finally { if (this.inFlight === work) this.inFlight = null; }
  }
  checkForUpdateInformation() { return this.check(); }
  async checkForUpdates() {
    const result = await this.check();
    const current = this.options.offers.options.buildVersion;
    if (result.status === 'available') {
      const choice = await this.electron.dialog.showMessageBox({type: 'info', noLink: true,
        title: this.text('Codex 有新版本', 'Codex update available'),
        message: this.text('发现官方新版本：', 'Official update available: ') + result.availableBuildVersion,
        detail: this.text(`当前版本：${current}\n先更新官方应用；启用补丁监测后，兼容性检查通过并退出 Codex 时会自动补装。`,
          `Current version: ${current}\nUpdate the official app first. With the patch monitor enabled, the matching patch is reapplied after compatibility checks and Codex exits.`),
        buttons: [this.text('打开官方更新', 'Open official update'), this.text('稍后', 'Later')], defaultId: 0, cancelId: 1});
      if (choice.response === 0) await this.openStore();
    } else if (result.status === 'up_to_date') {
      await this.electron.dialog.showMessageBox({type: 'info',
        title: this.text('Codex 更新检查', 'Codex update check'),
        message: this.text('当前已是官方最新版本', 'Codex is up to date'),
        detail: this.text('已检查官方发布源。当前版本：', 'Checked the official release feed. Current version: ') + current});
    } else {
      await this.electron.dialog.showMessageBox({type: 'warning',
        title: this.text('Codex 更新检查', 'Codex update check'),
        message: this.text('本次未能完成版本检查', 'Could not check for updates'), detail: result.reason});
    }
  }
  async checkForUpdatesInBackground() {
    const result = await this.check();
    if (result.status !== 'available' || this.notified.has(result.availableBuildVersion)) return;
    const Notification = this.electron.Notification;
    if (!Notification?.isSupported()) return;
    try {
    const notice = new Notification({title: this.text('Codex 有新版本', 'Codex update available'),
      body: this.text('官方版本 ', 'Official version ') + result.availableBuildVersion +
        this.text(' 已发布。点击打开官方更新。', ' is available. Click to open the official update.')});
    notice.on('click', () => { void this.openStore(); });
    notice.show(); this.notified.add(result.availableBuildVersion);
    } catch { /* Notifications are optional; menu checks remain available. */ }
  }
  async openStore() {
    if (!this.options.allowed()) return;
    const id = this.options.storeProductId;
    const url = /^[A-Za-z0-9]+$/.test(id || '') ? 'ms-windows-store://pdp/?ProductId=' + id
      : 'ms-windows-store://downloadsandupdates';
    try { await this.electron.shell.openExternal(url); }
    catch (error) { await this.electron.dialog.showMessageBox({type: 'error',
      message: this.text('无法打开官方更新入口', 'Could not open the official update'),
      detail: this.text('请从原安装渠道更新官方 Codex。', 'Update the official Codex app through its original installation channel.')}); }
  }
  // The native install RPC can be called independently of the menu.
  // Recheck before offering the official installer; never signal a staged MSIX.
  installUpdatesIfAvailable() { return this.checkForUpdates(); }
}
