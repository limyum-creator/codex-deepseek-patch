/* Native, non-activating macOS update panel. No browser or third-party runtime. */
ObjC.import('AppKit');
ObjC.import('Foundation');

function run(argv) {
    if (argv.length < 3) throw Error('Expected state directory, Python and controller paths');
    const directory = argv[0], python = argv[1], controller = argv[2];
    const loopTest = argv.indexOf('--self-test-loop') >= 0;
    const selfTest = argv.indexOf('--self-test') >= 0 || loopTest;
    for (const path of [directory, python, controller]) {
        if (path[0] !== '/' || /[\x00\r\n]/.test(path)) throw Error('Absolute paths required');
    }
    const file = directory + '/progress.json';
    let state = null, closing = false, currentAction = null;
    const app = $.NSApplication.sharedApplication;
    const brand=$.NSImage.alloc.initWithContentsOfFile($(directory+'/settings-ui/brand.png'));
    if(brand)app.applicationIconImage=brand;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    function readState() {
        const raw = $.NSString.stringWithContentsOfFileEncodingError($(file), $.NSUTF8StringEncoding, null);
        if (!raw) return null;
        try {
            let value = JSON.parse(ObjC.unwrap(raw));if(value.display&&value.display.id===value.id&&value.display.edition===value.edition)value=Object.assign({},value,value.display);
            if (value.format !== 1 || typeof value.id !== 'string' || !value.id || value.id.length > 200) return null;
            return value;
        } catch (_) { return null; }
    }
    function action(kind) {
        if (!state || ['start','launch','dismiss','cancel'].indexOf(kind) < 0) return;
        if (kind === 'start' && !state.can_start || kind === 'launch' && !state.can_launch) return;
        const task = $.NSTask.alloc.init;
        task.launchPath = $(python);
        task.arguments = $([controller, '--state-dir', directory, '--action', kind, '--id', state.id, '--phase', String(state.phase || '')]);
        const sink = $.NSFileHandle.fileHandleForWritingAtPath('/dev/null');
        task.standardOutput = sink; task.standardError = sink;
        task.launch;
        if (selfTest) task.waitUntilExit;
        if (kind !== 'dismiss') { primary.enabled = false; currentAction = kind; }
    }
    ObjC.registerSubclass({
        name: 'CodexAdapterProgressController',
        superclass: 'NSObject',
        protocols: ['NSWindowDelegate'],
        methods: {
            'primaryClicked:': {types: ['void', ['id']], implementation: function (_) {
                action(state && state.can_launch ? 'launch' : state && state.can_cancel ? 'cancel' : 'start');
            }},
            'dismissClicked:': {types: ['void', ['id']], implementation: function (_) {
                panel.close;
            }},
            'pollState:': {types: ['void', ['id']], implementation: function (_) {
                render(readState());
                if (loopTest && !closing) dismiss.performClick(null);
                if (closing) app.terminate(null);
            }},
            'windowWillClose:': {types: ['void', ['id']], implementation: function (_) {
                closing = true;
                try { action('dismiss'); } catch (_) {}
                if (!selfTest || loopTest) app.terminate(null);
            }}
        }
    });
    const receiver = $.CodexAdapterProgressController.alloc.init;
    const panel = $.NSPanel.alloc.initWithContentRectStyleMaskBackingDefer(
        $.NSMakeRect(0, 0, 460, 236),
        $.NSWindowStyleMaskTitled | $.NSWindowStyleMaskClosable | $.NSWindowStyleMaskNonactivatingPanel,
        $.NSBackingStoreBuffered, false);
    panel.title = 'Codex 模型补丁更新';
    panel.releasedWhenClosed = false;
    panel.hidesOnDeactivate = false;
    panel.floatingPanel = false;
    panel.becomesKeyOnlyIfNeeded = true;
    panel.delegate = receiver;
    panel.center;
    const content = panel.contentView;
    function label(frame, size, bold) {
        const field = $.NSTextField.alloc.initWithFrame(frame);
        field.editable = false; field.selectable = false;
        field.bezeled = false; field.drawsBackground = false;
        field.font = bold ? $.NSFont.boldSystemFontOfSize(size) : $.NSFont.systemFontOfSize(size);
        field.lineBreakMode = $.NSLineBreakByWordWrapping;
        content.addSubview(field);
        return field;
    }
    const title = label($.NSMakeRect(24, 177, 412, 30), 17, true);
    const edition = label($.NSMakeRect(24, 152, 412, 22), 12, false);
    edition.textColor = $.NSColor.secondaryLabelColor;
    const detail = label($.NSMakeRect(24, 95, 412, 48), 13, false);
    const progress = $.NSProgressIndicator.alloc.initWithFrame($.NSMakeRect(24, 76, 412, 14));
    progress.style = $.NSProgressIndicatorStyleBar;
    progress.minValue = 0; progress.maxValue = 100;
    content.addSubview(progress);
    const dismiss = $.NSButton.alloc.initWithFrame($.NSMakeRect(20, 19, 102, 32));
    dismiss.title = '关闭窗口'; dismiss.bezelStyle = $.NSBezelStyleRounded;
    dismiss.target = receiver; dismiss.action = 'dismissClicked:'; content.addSubview(dismiss);
    const primary = $.NSButton.alloc.initWithFrame($.NSMakeRect(290, 19, 150, 32));
    primary.title = '开始更新'; primary.bezelStyle = $.NSBezelStyleRounded;
    primary.target = receiver; primary.action = 'primaryClicked:'; content.addSubview(primary);
    function render(value) {
        if (!value) return;
        if (state && state.id !== value.id) { closing = true; return; }
        state = value;
        title.stringValue = String(value.title || '正在准备更新').slice(0, 160);
        edition.stringValue = (value.edition === 'ds' ? 'DS 版' : '扩展版') + (value.version ? ' · ' + String(value.version).slice(0, 80) : '');
        detail.stringValue = String(value.detail || '关闭此窗口不影响后台更新。').slice(0, 500)+(value.total?' · '+Math.floor(100*value.completed/value.total)+'%':'');
        const known = Number.isFinite(value.completed) && Number.isFinite(value.total) && value.total > 0;
        const terminal = ['current','completed','failed','rolled_back','close_failed','error','launched'].indexOf(value.phase) >= 0;
        progress.indeterminate = !known && !terminal;
        if (terminal) {
            progress.stopAnimation(null);
            progress.doubleValue = value.phase === 'completed' ? 100 : 0;
        } else if (known) {
            progress.stopAnimation(null);
            progress.doubleValue = Math.max(0, Math.min(100, value.completed * 100 / value.total));
        } else progress.startAnimation(null);
        primary.title = value.can_launch ? '启动 Codex' : value.can_start ? '开始更新' : value.can_cancel ? '取消等待' : '正在更新…';
        primary.enabled = !!(value.can_start || value.can_launch || value.can_cancel) && !(currentAction === 'start' && value.can_start);
        primary.hidden = !(value.can_start || value.can_launch || value.can_cancel) && ['current','completed','failed','rolled_back','close_failed','error','launched'].indexOf(value.phase) >= 0;
    }
    render(readState());
    if (!state) throw Error('Update state unavailable');
    app.finishLaunching;
    if (selfTest && !loopTest) {
        // Build and populate actual native widgets without showing a window.
        if (argv.indexOf('--self-test-action') >= 0) primary.performClick(null);
        if (argv.indexOf('--self-test-dismiss') >= 0) panel.close;
        return JSON.stringify({title:ObjC.unwrap(title.stringValue),detail:ObjC.unwrap(detail.stringValue),
            primary:ObjC.unwrap(primary.title),enabled:!!primary.enabled,indeterminate:!!progress.indeterminate,
            visible:!!panel.visible,nonactivating:true,action:currentAction,protocol:'progress-v1'});
    }
    if (!loopTest) panel.orderFrontRegardless;
    // AppKit owns event dispatch so native mouse tracking, close controls and
    // accessibility actions keep running while a timer refreshes update state.
    const timer = $.NSTimer.timerWithTimeIntervalTargetSelectorUserInfoRepeats(
        0.4, receiver, 'pollState:', null, true);
    $.NSRunLoop.mainRunLoop.addTimerForMode(timer, $.NSRunLoopCommonModes);
    app.run;
    timer.invalidate;
}
