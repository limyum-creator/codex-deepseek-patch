## Unified 0.9.0 and Issue #5

The product now has one release and two platform receipts. Existing DS/proxy installation identities remain internal ownership affinities; both legacy upgrade paths must preserve credentials, model choices/hiding, context/compaction reductions, service ownership and rollback. Their first compatibility packages share the unified core. New checks use `main/unified-update.json`; bridge equivalence requires exact runtime-file hashes and the same version. Test the next canonical upgrade and rollback, not only initial bridge download.

External message IDs now carry a versioned origin. Same-provider requests recover the original identity; cross-provider full messages are replayed inline, preserving content and function `call_id` linkage. Old complete UUIDv4 adapter messages are handled even without reasoning items. A same-model reasoning restore is not evidence of a provider switch. Test JSON/SSE event ID consistency, HTTP/WS, no reasoning, mixed reasoning, native GPT ID preservation and real native same-thread return to GPT. Provider error reporting remains unchanged.

The release requires the unified migration/source tests and `native_cross_provider_message_ids` on both OSes. Historical sections below describe earlier releases and their then-current four-variant matrix.

## Builtin installation validation regression (Issue #3)

`validate_build()` must run directly for fresh builtin ownership (`provider=None`), legacy provider tables, and builtin migrations retaining a dormant table. Invalid/missing ownership, provider types, selected provider, endpoints, health identity, service command, staged hashes, app identity and failed probes remain rejected. First-install transaction fixtures must have a path that keeps the production validator enabled.

The build verifier now runs this same installation validator before saving success. Every release receipt additionally requires a fresh isolated `build --bundled-catalog --verify` with the real signed app, no existing configuration/provider table, and `native_install_validation=passed`. Both editions on macOS and Windows must produce the content-bound prepared-build log. This prepares and checks an isolated build without activating it on the user's system.

## Next development candidate: bootstrap and Messages

Before release, validate both editions on macOS and Windows using ordinary-user, hidden/private-desktop fixtures. Additional mandatory regressions cover bootstrap digest rejection, atomic staging, channel isolation, stable runtime after deleting the original download, first-setup progress/cancellation, pending-recovery ownership, Messages text/images/tools, HTTP/WS translation, truncated SSE, cached token usage, reasoning signatures and foreign-model isolation. Native CLI tests include GPT → Anthropic lazy tool discovery/execution → GPT and old encrypted-history recovery through Messages.

Installer artifacts have a separate runtime inventory and vendor SHA256 pin. Test the exact bundled interpreter and native setup card; source-only tests do not establish bootstrap usability. CPU variants not exercised on hardware remain unvalidated. Public Mac installer signing/notarization and downloaded-file launch must pass before distributing that artifact. Generated binaries never enter the source feed.

Protocol references: [Messages](https://platform.claude.com/docs/en/api/messages/create), [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming), [model metadata](https://platform.claude.com/docs/en/api/models/list). No real provider inference or media generation is performed by these synthetic checks.

## First setup, request diagnostics and specification sources

The next candidate must pass first-install native cards on both systems and editions, manager-conflict rejection before credentials/network, failed discovery secret rollback, revision conflicts, cancellation, unverified install refusal, and resuming monitor registration after installation. Use disposable homes and mocked activation; never install into the developer's current model manager.

Recent request diagnostics must exercise the existing HTTP/WebSocket inference paths with synthetic bodies, authenticate their local endpoint, omit bodies/credentials/full URLs, bound retained records, and distinguish upstream HTTP errors. Native GPT forwarding, cancellation and payload fidelity gates remain mandatory. Local-only request metadata is not added to exported diagnostic reports.

Specification provenance is descriptive: test service metadata, explicit declarations, inferred capabilities and conflicts without changing context precedence or user reductions. Check first-install and synchronized catalogs separately.

## 后续版本的强制发布检查

### 预下载、空闲安装与事务回退

压缩管道还必须覆盖延迟开始读取的大输入，以及输入尚未发送完成时取消并回收子进程。Python 3.11 的短超时 communicate 重试可能丢失后续 stdin 写入注册；压缩改为单次读写配合取消等待，保持字节完整。

四组合发布门禁新增：真实字节进度（含无 Content-Length）、同阶段计数不回退、旧进度写入隔离、单次点击跨等待继续、工具尚未返回时禁止安装、空闲 WebSocket 不阻塞、排空锁与新请求竞争、取消与一小时到期保留候选、旧协议等待正常退出、启动失败恢复旧运行时、配置与服务回退、独立修改阻止覆盖。AppKit/WPF 实际控件分别检查 25% 显示、取消等待按钮及完成后的启动按钮。

每次发布继续运行原生 CLI 的 GPT → DS →（扩展版 Grok/Gemini/本地接口）→ GPT、额度耗尽后的跨供应商压缩与工具、原生生图、登录隔离和 Windows 后台退出用例。测试使用隔离服务和合成凭据，正式运行中的应用与用户任务不参与退出/回退测试。


同对话切换须分别覆盖未压缩的 GPT → DS → GPT，以及 GPT 原生加密压缩后 → DS 恢复历史、调用工具 → GPT。历史摘要的异常回归必须包含 HTTP 200 SSE 内的输出截断、内容过滤、额度不足、限流、鉴权失败及未知失败。输出截断须验证分段恢复保留全部 UTF-8 输入、仅缓存完整结果、取消有效且请求次数有界；其他失败不在适配层反复提交。错误保留可操作的分类，不复制上游提示词或凭据。上述共享代码检查随两系统、两版本的完整测试一起运行。

## 大请求与原生行为回归门槛

两系统、两版本均须通过：原生 ChatGPT 登录模式的 zstd 请求、超过 64 MB 的合成图片历史发送及手动压缩、图片与鉴权保真、WebSocket/HTTP 路径，以及上游 413 原样返回且适配器不自行重试。真实客户端只连接隔离服务，测试不读取用户图片、不产生真实模型费用。

同时检查旧版压缩开关迁移、用户主动关闭压缩、安装失败回滚、GPT 官方窗口比例和压缩规则、用户自定义超过 90% 的阈值、外接模型长时间思考与取消。可选外接生图工具另测超过五张参考图、16 MB 输入图和 64 MB 响应。发布器检查这些用例的实际通过记录；测试缺失、跳过或失败时不得晋级更新通道。


发布器逐项校验四份记录：macOS × DS、macOS × 扩展版、Windows × DS、Windows × 扩展版。记录绑定同一候选源码和各自的更新通道；缺项、跳过、失败或记录被改动，均阻止发布。单元测试数量不作为功能完整性的判据。

云端 Windows 固定使用 Windows 2022，Mac 使用 macOS 15 Intel，继续运行完整检查；实际 Apple Silicon Mac 与 Windows 电脑的四份原生验证记录独立必需。固定云端镜像避免 `latest` 迁移与 ARM 托管队列同时改变验证环境。

| 范围 | 每次发布必须覆盖 |
| --- | --- |
| 原生身份 | 原版客户端、原登录凭据传递、原有聊天迁移、指令预判所需的身份及分叉 |
| 聊天与模型 | 同一聊天多模型切换、思考档位、默认最大上下文、用户主动缩减、自动与手动压缩 |
| 工具 | 按需加载大量工具、整数参数、自定义工具、原生生图与改图、参考图传递、图片落盘 |
| 传输 | 原生 HTTP 服务原样转发、WebSocket 压缩协商及长历史、解压上限、真实心跳帧转发、关闭码保留、错误及时结束、取消、认证隔离 |
| 历史兼容 | 原 GPT 压缩历史切到外部模型、额度用尽、恢复后再次调用工具 |
| 设置与更新 | 两端原生设置入口、隐藏与唤醒、检查后开始更新、已安装记录接续、启动失败后重试、后台检查保留终态、两通道隔离 |
| 扩展版额外项 | 本地兼容接口、Chat Completions、不同工具格式往返及返回 GPT |

原生生图检查使用随应用附带的真实 CLI，让它实际调用 `imagegen` 的生成、编辑功能，经候选适配层到隔离服务，再检查原生工具完成事件和保存的图片文件。使用合成登录、临时 TLS 证书与测试图片，结果标记为 `native-client-with-synthetic-services`；联网生成结果另行记录。测试所需 OpenSSL 仅用于发布验证，普通安装和运行不增加此依赖。

每次复现的新故障都要加入必测清单和失败分支。例如本次新增了原生图片接口、心跳帧、关闭后原生重试、服务报错后及时结束、检查更新后记录编号变化、启动失败恢复按钮的检查。发布器保存并核对 `coverage.json`，同时要求两端原生设置检查及各自平台的唤醒检查通过，而不仅检查测试进程是否退出成功。

两台实际机器的安装状态另行核对；用户仍在运行任务时，只准备更新，待正常退出后安装。

macOS 服务切换还使用随机名称的真实 launchd 测试服务：主动延迟旧进程退出，连续验证更新、回滚及卸载，确认旧任务退出后才注册新任务。测试不停止正在使用的适配器。定时身份检查同时覆盖成功与失败路径，均不得覆盖已完成、失败、等待退出或正在安装的更新状态。

## 鲸鱼设置窗口驻留与唤醒回归

- Mac 快捷键测试向真实 Carbon 队列投递事件，验证分发进入已注册的处理器并写出唤醒请求。
- Windows 在隔离桌面以普通用户运行正式窗口入口，检查持续显示、隐藏、重新呼出、打开偏好、损坏唤醒记录容错和正常退出。该检查不依赖渲染器的 self-test 分支。
- Windows 另外向正式 WPF 控件投递鼠标和点击事件：检查拖动与释放捕获、六个分类的命中区域与悬停说明、实际点击进入页面、模型行与返回、保存偏好。页面测试必须经过按钮注册的事件处理器，不能只直接调用打开页面函数。
- Windows 设置目录显式继承当前用户 SID，避免同一用户以管理员权限创建 IPC 文件后，普通用户窗口失去读取权限。
- 浮窗崩溃恢复限制重试次数；错误尾部仅保存在本机，诊断导出不包含原始错误文本。App 入口启动后及时返回，重复点击会唤醒现有实例。
- 两端页脚共用行列尺寸，覆盖长按钮、返回按钮和多行操作；Mac 绘制控件保留至页面释放，页面正文载入后也执行文字入场动画。

# Whale hub and integrated updates

Native motion checks exercise opening and closing cards, hover reveal, delayed retraction, keeping an editing form open, detaching by drag, all four display edges and pointer directions, release/re-entry hysteresis, arrival hints, reduced motion and stopping idle animation timers. Regression coverage includes numeric edge-distance sorting on Windows and removing the Mac spray layer after completion or cancellation. Small generated PCM sounds are format/level/duration checked and loaded by native players; self-tests stay silent. Both hosts use tangent-continuous cubic edges and rounded corners throughout the transition; final corners match the card styling. Visual review includes actual AppKit and WPF output, not only event-loop completion.

AppKit transitions now reuse an in-memory image while animating the liquid outline, then return to live native controls. Menu icons use bounded 2× raster caches; model rows are created when entering the viewport, with search and end-of-list scrolling covered by a 40-model native fixture. Unchanged pages are reused, and asynchronous data refreshes wait until the transition ends. Windows already transforms its WPF content as a visual subtree; its independent regression suite is retained.

An opt-in, headless AppKit rendering probe (`CODEX_WHALE_PROFILE=1` with the native self-test) measures callback work plus forced bitmap rendering on the same 40-model fixture. Before/after means were about 428/9 ms for card frames and 35/12 ms for menu frames. This measures rendering work rather than display refresh rate; timings are diagnostic data, not a machine-dependent pass threshold.

The native UI now shares compact layout data across AppKit and WPF. Progress, manual update start, launch-after-completion, update options, independent model synchronization and maintenance use the same singleton host. A background notice reveals only the whale badge; the Updates drop uses a pale-red attention state until completion. No standalone update viewer is launched by the current presenter.

Focused tests cover update identity/edition isolation, stale action rejection, notification de-duplication, reuse of an existing/hidden host, real work counters, explicit start/launch, and native available/completed badge/button states. Actual preview controls are checked against disposable state; Windows tests run on an isolated desktop under an ordinary user. Existing installation and live chats remain separate from UI testing. A legacy settings window retains its unsaved form during the first upgrade. One windowless handoff launcher waits for that window to close before opening the whale; concurrent notices/reopen requests coalesce, with explicit navigation taking priority. An already-open legacy progress viewer finishes the first upgrade without a duplicate whale notification; after it closes, an explicit reopen uses the whale. Regression tests exercise the real cross-platform state locks and retain settings/config bytes. Full paired release receipts and CI are required before publication.

---

# Settings launch and native stream ownership

2026-10-20. Follow-up checks reproduced two production launcher defects. Finder could launch JXA through Rosetta and stall in libffi callback initialization; Windows startup flags intended to hide a console also hid the first WinForms window. Native UI launch now pins the Mac host architecture and uses Windows `CREATE_NO_WINDOW` without a hidden-window startup override. A private-desktop Windows test verifies the actual window visibility, and both native event-loop tests click Refresh and close through the window callback.

The official GPT relay no longer imposes a shorter 120-second stream timeout. The native client owns inference/compaction idle deadlines and cancellation; transport connection establishment remains bounded. Delayed HTTP and early/late WebSocket tests exercise this distinction in both editions. A silent HTTP cancellation closes the upstream socket. The exact official standalone compaction endpoint retains original authentication and request bytes; external routes and arbitrary paths remain rejected.

Read-only WorkPC evidence showed the reported conversation completing native compaction after WebSocket retries and HTTP fallback. Its window remained 872,000 before and after compaction. Independent small live checks on Mac and Windows completed a native GPT turn, experimental prediction fork and nonempty JSON suggestion with the installed 0.6.0 adapter. Desktop display eligibility remains a separate investigation; no prediction gate or sampling policy was overridden.

The code-prompt/blue-whale mark is a small source SVG. Launcher icons are generated locally with Python's standard library; no image runtime or binary application bundle is shipped. Update panels explicitly identify the model patch and edition.

---

# Settings, maintenance and native feature preservation

2026-10-20. The coordinated DS 0.5 / extension 0.6 candidate adds an on-demand AppKit/WinForms settings window, queued update checks, verified archive downloads, conservative owned-cache cleanup, redacted diagnostics and cancellable old-history recovery status.

- Native CLI fixtures pass on macOS and ordinary-user Windows in both editions: legacy-provider metadata migration to builtin `openai`, prediction fork eligibility, original authentication, GPT–DS–Grok–Gemini–GPT switching, local Chat Completions, reasoning selection, 160 tools, native custom tools, per-model windows and automatic/manual compaction.
- External compaction produces a self-contained readable handoff through the selected service. Native Codex still schedules compaction and installs its history. GPT encrypted history uses the existing selected-model recovery path when switching away; fixtures verify continuation after GPT quota exhaustion.
- Native HTTP services under `/backend-api/codex/` preserve authentication, method, query and opaque bytes on the fixed official origin. Unknown future service paths are covered alongside image generation/editing and search; external provider credentials and model routing never enter this path. WebSocket compression tests include independent RFC vectors, context takeover, large history, fragmentation, optional negotiation and bounded decompression.
- Settings tests exercise private asynchronous requests, window close, actual native controls, input validation, stale-edit conflicts, rollback on failed writes, context reset, service toggles, saved reductions/hiding, secret-free diagnostic reports, and owned launcher lifecycle. Routine tests do not show production windows or change system execution policy.
- Paired candidates require content-bound native/unit receipts for all four combinations plus source CI. Independent channel versions and sequences remain intact. Archive corruption never falls back to unverified downloads; cleanup retains active, recent and recovery-referenced builds.
- Dot reconnecting on the development Mac was traced separately to conflicting system DNS routing. A user-applied domain-specific resolver restored the original native connection without modifying Codex or disabling Tailscale. This machine-specific resolver is not installed by the patch.

Private receipts, native results and UI captures are retained outside release files. Final receipts describe exact candidate validation; machine installation is verified separately.

---

# Preserve moved workspaces during metadata migration

2026-10-19. A legacy archived chat exposed native resume falling back to its original rollout directory after a later desktop workspace move. Migration now records the complete protected metadata snapshot and supplies the saved cwd explicitly. Windows path comparisons normalize separators and verbatim prefixes while retaining folder case and all other metadata checks. Interrupted older journals require an exact matching original snapshot before recovery.

WorkPC recovery through native metadata APIs verified 106 original chats, providers, archive states and workspace identities. No SQL writes or inference turns were used. The repair also persists recovered snapshots before retrying a metadata change.

---

# Legacy configuration metadata entry

2026-10-19. Pre-install metadata inspection now avoids creating an incomplete adapter provider and uses literal native override keys for existing MCP names. A production legacy Mac configuration passed initialize/thread-list with tools and inference disabled; isolated before/after provider configurations are regression-covered. The configuration on disk is unchanged by this inspection.

---

# Update panel interaction repair

2026-10-19. macOS now uses the native NSApplication event loop with timer-based state refresh. The close button was confirmed by an actual user click; a hidden native event-loop test also exercises the close callback. Failed/completed panels stop animating on both platforms.

The macOS stopped-app check excludes only an orphan Electron browser_crashpad_handler in the target Frameworks/Helpers path. The application, CLI and other active helpers still block installation. No application process is killed by this check.

---

# Official-client refactor: validation record

2026-10-19. DS 0.4.0 and extension 0.5.0-proxy.1 use the original desktop app plus a small external adapter. Results below distinguish real model calls from synthetic protocol fixtures. The older records remain below as historical evidence.

## 0.5.10 / 0.4.10: recover old GPT history without GPT quota

- 533 unit tests pass on macOS and ordinary-user Windows (12 / 8 platform skips). Windows tests ran with no console window.
- The actual native Mac and Windows CLI resumed an isolated chat seeded with an old encrypted compaction event while the official GPT fixture was quota-exhausted. Both edition suites completed selected-model summaries, same-chat continuation and native tool execution with zero GPT requests; the original rollout prefix remained unchanged.
- Extension fixtures cover DS, Grok, Gemini and local Chat Completions routes. Existing tool discovery, same-chat switching, reasoning selection and post-compaction context checks remain in the same suite.
- Small live checks with existing DS Flash, Grok 4.7 and Gemini Flash endpoints recovered a synthetic goal/tool marker and returned it correctly with the GPT endpoint disabled. No real user history was sent by these live checks.
- Read-only verification of a long local fork reconstructed its bounded parent history and current prefix in under a second. Unit cases cover wrong/missing chat identity, fork byte/ordinal boundaries, missing prefixes, malformed files, symlinks, cancellation, small-window reduction and reuse of old cache entries.
- Package edition and version are read from one edition.json authority. Fresh subprocess tests exercise both DS and extension metadata, so copying shared source cannot change the update channel.
- No app bundle, login store, history database or original conversation file is rewritten by recovery. Unsupported or incomplete local history returns an explicit recovery error; it never falls back to GPT.

## 0.5.8 / 0.4.8: exit-triggered application and quota regression

- Service readiness tolerates a transient response from the previous adapter while the OS completes its service handover; only the new exact identity passes, and a persistent mismatch still fails within the deadline.
- The install worker preserves a typed app-reopened outcome across its subprocess boundary, retaining the prepared build and waiting progress instead of reporting a terminal failure. A real child-process regression exercises the status transport.
- A prepared update now arms one temporary process-exit observer (macOS kqueue / Windows process handles). Real process-exit tests wake immediately; singleton, fast-reopen and disabled-monitor cases pass. Exact orphan crash/keyboard helpers no longer prevent a Mac update after the main app exits.
- 517 local unit tests pass (12 platform-specific skips); the ordinary-user Windows windowless updater probe passes 120 tests with no console window.
- On the actual bundled Mac and Windows native CLI, both edition fixtures receive a synthetic GPT `usageLimitExceeded` failure, then complete independent-model turns in the same chat without another GPT request. Extension fixtures cover DS, Grok, Gemini and a local Chat Completions route. GPT works again after the fixture quota resets. These are isolated protocol tests, not an exhausted real-account desktop UI session.
- An additional HTTP-429 regression confirms the remaining legacy boundary: uncached proprietary GPT compaction needs the official summary service and fails before contacting the external provider when that service has no quota. Ordinary visible history and cached summaries still work. This shared boundary applies to both platforms and editions.

## Real models and original authentication

- macOS and Windows each completed the same-chat **GPT → DeepSeek → Grok 4.7 → Gemini 3.8 Flash → GPT** sequence. Every turn recalled all earlier synthetic tags; DS/Grok/Gemini executed the native file tool; the final GPT recalled the complete sequence and file marker without a tool.
- The first two diagnostic runs exposed foreign encrypted reasoning and foreign reasoning-content schema leaking into GPT. Model provenance isolation and readable-message normalization fixed both; the complete rerun passed on both platforms.
- Tests used the existing ChatGPT access only in an isolated CLI's ephemeral credential store, without refresh or production configuration/history changes. No credential was found in the disposed test state.
- A separate real GPT/DS/Grok test passed native readable-summary compaction and GPT return. A synthetic old GPT compacted-state fixture also exercised the real one-time readable-summary conversion and cache reuse.

## Native CLI and protocol coverage

The untouched bundled CLI exercises both editions with fake loopback model endpoints. Checks include model catalogs, high/low/high reasoning, per-model context beyond 128K, automatic/manual readable compaction, same-chat switching, original GPT WebSockets and provider credential isolation, local prewarm, 160 lazily discovered MCP tools, integer argument normalization and actual custom-tool file creation.

The extension additionally tests native CLI → Chat Completions → native lazy tool execution and custom tool execution → GPT. Independent HTTP/SSE/WebSocket fixtures cover anonymous local services, arbitrary Claude/GLM/local model names, images, parallel calls, result association, truncation, cancellation, errors and unsupported capabilities. Those arbitrary models have **synthetic protocol coverage**, not credential-backed real-model validation.

The original Mac host's vendor signature, bundled CLI/Node identity, native app tool connection and 53-tool registration were checked. The previously installed legacy Mac app completed an actual Calculator computer-use test; this refactor's new host has not yet repeated that full GUI interaction. Routine verification uses background CLI processes and does not launch desktop test windows.

## Installation and migration

- Windows normal-user pythonw tests verify hidden execution, native metadata migration/restart/reentry/rollback, preserved model/effort/archive/content, and zero inference during migration.
- Fault-injection tests cover activation phases, configuration rebase, later unrelated edits, independent rollback failures, commit interruption, idempotent recovery and restoring current chat metadata after extended use.
- Actual Windows Task Scheduler fixtures restore Ready, Disabled and Running states exactly. UTF-16 XML declaration/encoding and CRLF backup restoration are covered.
- Windows package activation was verified through the original Store AUMID and real package identity APIs. New installation paths never copy the Store app.

## Legacy updater and notification regression

The published extension 0.4.10 updater was exercised on Windows with its parent working directory inside the runtime. This reproduced a directory-lock failure during child activation. The fixed independent native recovery record survives the old updater deleting its activation journal; both immediate parent promotion and injected promotion failure followed by restart restore the native baseline and backend.

A subsequent full Windows legacy-package preflight exposed console flashes from Node/OpenSSL child processes inside tests. A windowless Python parent does not hide those grandchildren. All real test subprocesses now request hidden startup explicitly; a source contract checks this across the full test directory. This is required for both editions, since old updaters run the complete downloaded test suite.

### Final Windows and four-variant gates

- Published old updater, fresh candidate download path: 91/91 source-file digests checked; **484 Python tests passed** (5 platform/tool availability skips) and the legacy Node route suite passed.
- A normal-user WinEventHook observer covered that entire run: **zero owned terminal/form show events**, zero other show events, and no observer errors.
- macOS and Windows native suites passed for both editions: DS **9 checks**, extension **12 checks**. Windows uses danger-full-access only inside this deterministic synthetic test's disposable home/work directory, records the actual effective policy, and leaves production settings untouched. Integer tool execution requires completed status, exit code 0 and a standalone output marker; custom tools require completed file changes and exact expected file contents.
- The final Windows DS suite repeated the window observer with zero show events. The extension's real-model five-step sequence and synthetic Chat Completions suite remain separately recorded above.
- macOS full suites passed in both branch configurations (483 tests at that checkpoint, 10 Windows skips); later Windows hidden-child enforcement and tool-success regressions were checked separately. Hidden native panels passed four AppKit cases and eleven Windows control/contract cases without showing production windows.

## Lightweight runtime

A private macOS adapter process serving only a health check measured **166,046 bytes across 9 runtime source files** and **31,120 KiB idle RSS** before the later transport close fix. This measures that specific build/process, excluding Python, the official app, test/recovery/update sources, external services and model weights. It does not measure peak inference memory.

The optional update panel uses system AppKit / WinForms, with no web frontend or third-party UI dependency. Native control tests are performed without showing windows or closing production applications. Completion never launches Codex without the user pressing the launch button.

---

# v0.4.10-proxy.1：恢复 Windows 官方应用版本检测

2026-10-19。Windows 受管副本不具备当前进程的 MSIX 包身份，官方更新器因此在初始化阶段退出。本次仅在 Windows 且原因精确匹配时启用兼容检查器，复用官方发布源、schema、版本比较及架构选择，保留原更新策略。

- 单元回归 240 项，其中新增 20 项覆盖真实适配器执行、错误分支、策略撤销、并发检查、通知、安装入口以及源码契约和资源完整性。Mac 下 4 项 Windows 专属测试跳过。
- 用实际 Windows 官方 offers class、Zod schema 和版本比较函数进行 9 项受控回放：实时官网下载的当前版本 JSON、较新、相等、较旧版本、HTTP 错误、HTTPS 降级、错误 schema、缺架构、策略撤销，全部通过。网络层注入实时公开快照；没有调用需要包身份的原生更新 API。
- 检查结果为 available / up_to_date / error / unavailable；发现新版可打开官方安装入口。只发现新版时不宣称安装包已下载，不设置 ready，避免触发原生强制退出安装。官方应用更新后由原有补丁监测接续兼容性检测和补装。
- Windows 两版均适用，使用相同修复，DS 和扩展保持独立发布及更新源。Windows 构建只改变已声明的客户端路由及 updater 两个 ASAR 成员，原 Store 来源保持只读。
- Mac 两版不插入这个 Windows 检查器；本次对实际官方 Mac 来源执行兼容性检查，并复核既有补丁 ASAR 的全部 21,174 个成员，原生 Sparkle 代码保持一致。
- Mac 当前安装的扩展版 0.4.9-proxy.1 已实际通过 Computer Use：读取计算器、点击、输入 17+25、回车得到 42、截图，随后恢复原显示。未修改系统权限。

---

# v0.4.9-proxy.1：Mac 隔离测试不再等待钥匙串弹窗

2026-10-19，针对“测试时没有可用钥匙串，未点击导致更新超时”的反馈，修正临时 HOME 下的 GUI 启动检查。测试专用模拟钥匙串只作用于隔离副本，真实登录钥匙串和正式应用的加密存储保持原样。

- 同一修改同步 DS / 扩展分支；Mac 两版的真实启动检查均通过，并从测试主进程确认 `--use-mock-keychain` 已生效，分别耗时约 6.38 / 6.41 秒。
- Windows 实际普通用户对话还发现认证助手无法写入由提升权限的诊断进程创建的日志；此前隔离 CLI 使用不同权限，未覆盖该条件。认证助手改为不写日志文件，并新增真实子进程启动、认证健康检查在禁止文件写入时仍成功的回归；安装验证使用交互式普通用户执行。Mac 扩展版共享相同修复，纯 DS 没有该兼容进程。
- macOS CI 的真实子进程回归进一步定位到 HTTPServer 启动时的反向 DNS 查询阻塞。仅绑定数字 loopback 地址的连接服务跳过该查询，新增禁止 DNS 查询的回归；Mac / Windows 扩展版共用修复，纯 DS 无此服务。
- Windows 不经过 macOS 钥匙串；回归覆盖 Windows / Linux 的启动参数保持原样，以及其他进程携带模拟参数不能满足本次检查。
- 本分支单元回归共 220 项；四组合核对规则已加入 CONTRIBUTING。既有 Grok 闲置连接、整数工具参数、首条消息和 Windows 隐藏自检修复保留。

---

# v0.4.8-proxy.1：闲置后 502 与工具数字参数

2026-10-19，Windows 实机对照复现同一 Grok 4.7 聊天闲置后的 502。兼容进程正常空闲退出，但旧配置 `refresh_interval_ms=0` 让原生 CLI 永久缓存 helper 结果，后续请求直接连接已关闭的端口。修正适配连接的刷新间隔，保持按需启动和空闲退出。

- 使用真实内置 CLI 和既有代理，在两个独立测试聊天中各完成首条回复；等待 128 秒，确认两个测试监听器均已退出，再在各自原聊天发送第二条。旧值 0 返回 502，修复值 1 自动恢复并完成回复；共四条简短推理请求。
- 独立原生缓存行为测试：三回合中旧值只调用 helper 一次，修复值逐回合调用，共三次。
- 真实 Grok 工具错误为 `floating point 30000.0, expected u64`。原生声明允许 `number`，执行器要求整数。按声明处理完整数字参数，精确保留数值；回归覆盖字符串、小数、未知字段、嵌套引用、命名空间和流式调用。
- 原生工具检查加入一次无副作用的 echo，实际传入 `yield_time_ms:1000.0` 和 `max_output_tokens:300.0`，检查 Codex 完成命令并返回标记。大工具集、发现、执行、压缩后窗口检查继续保留。
- Python 回归共 217 项：Mac 213 通过、4 项 Windows 专属跳过；Windows 实机 217 项全部通过。Mac 与 Windows 的真实原生工具执行检查均通过。构建器继续要求目录、思考、上下文、同聊天切换、大工具集和隔离应用启动全部通过才允许安装。
- DS 直连版没有闲置兼容监听器；它独立发布 0.3.6，同步首条消息和 Windows 无窗口自检修复，更新源互不覆盖。

---

# v0.4.7-proxy.1：首条消息与 Windows 无窗口检查

2026-10-19，原生复现新 Grok 对话发送首条消息前返回 `no rollout found for thread id`：此前模型切换逻辑会尝试恢复尚无持久记录的空对话。修复只对已加载、空预览、同一目标提供方且返回该对话确切记录缺失错误的情况，沿用创建好的会话发送；其他恢复错误继续正常报告。已有对话仍走原有的权限保留与上下文刷新流程。

- 新增真实原生首条消息检查，确认发送前不卸载会话，并保持模型的上下文窗口。实际 Grok 4.7 首条回复成功，窗口保持 500,000。
- Windows 调度、模型同步、凭据查询、更新 worker、验证及清理命令统一隐藏控制台，标准输入输出与 UTF-8 通信保留。
- Windows 桌面启动检查通过原生 `CreateProcessW` 的 `lpDesktop` 运行于本次专用桌面；不切换用户桌面，结束后释放。Python 的 `STARTUPINFO` 不提供该字段，因此此处显式调用原生接口。
- 全套 211 项 Python 回归；新增真实 Windows 无控制台管道、worker 结果传递和隐藏桌面生命周期检查，并在公司 Windows 机器通过原生隐藏桌面启动验证。

---

# v0.4.6-proxy.1：隔离验证的插件下载

2026-10-19，Windows 安装检查期间，原生 CLI 的后台插件克隆可能占用临时目录，使已通过的模型检查在清理时失败。

- 目录、思考、上下文、切换、工具、连接及启动的隔离检查关闭测试环境中的插件与远端插件自动下载；用户实际 Codex 的插件设置保持原样。
- 新增 CLI 参数和隔离桌面配置回归；全套 208 项测试覆盖图片工具及既有功能。
- Grok 生图复核：将代理登录与已验证的 Grok Build 账号对齐，使用其默认图片型号 `grok-imagine-image-quality`，现有 Images 接口单次请求成功返回一张 1024×1024 JPEG；同一账号的 Grok 4.7 聊天也正常。未改写代理路由或请求认证逻辑。
- 排查 Grok 图片权限或额度错误时，同时核对代理与 Grok Build 的账号、图片型号；本次先前失败使用的是另一账号及 `grok-imagine-image` 型号。保留原始错误记录，复核后明确标注实际成功的组合。

---

# v0.4.5-proxy.1：Windows 中文生图输入

2026-10-19，补充中文系统上的真实管道编码检查。旧图片工具会按 Windows 的 GBK 默认编码解读 Codex 发来的 UTF-8 请求；中文提示词或中文路径可能使工具退出。

- 图片 MCP 的 stdin/stdout 显式使用 UTF-8，保留系统区域设置和其他程序环境。
- 新增 GBK 子进程接收 UTF-8 中文 JSONL 的回归，验证提示词原文、中文输出目录和准确返回路径；同时保留 StringIO 测试兼容。
- Mac 与真实中文 Windows Python 3.12 的 29 项图片专项测试通过，无实际生图；全套 207 项回归继续覆盖既有模型、更新通道和安装行为。

---

# v0.4.4-proxy.1：可选图片生成工具

2026-10-19，为自定义聊天模型添加独立的 stdio 图片工具，直接复用既有 CPA 接口。

- 205 项 Python 回归（Mac 204 通过、1 项 Windows 专属跳过），包括 27 项图片接口、脱敏、输出保存和配置保护检查；Node 路由检查通过。
- 真实 Codex CLI 离线验证三路按需发现、MCP 调用、图片保存和内联图片回传，使用合成接口，无外部生成。
- Windows 实际账户验证：Gemini 聊天调用 Gemini 图片模型、Grok 4.7 调用 GPT Image 均完成一次图片生成并返回文件。
- Grok 4.7 能发起 Imagine 请求；测试 `grok-imagine-image` 型号时上游返回 `personal-team-blocked:spending-limit`。错误码被保留为可读提示；补丁不自动重试生成。
- CLI 配置启用、重复启用、更新与禁用有完整备份，保护已有其他 MCP、默认模型、多行 TOML 和 Windows 换行；正式配置沿用用户审批规则。
- 图片请求分别使用 GPT/Grok Images 和 Gemini generateContent，模型可用性按现成账户查询，返回 PNG/JPEG/WebP 新文件和图片内容。
- 功能随扩展版独立更新，DS 直连通道保持独立。

---

# v0.4.3-proxy.1：固定 macOS 签名与更新授权

2026-10-19，修复每次临时重签导致的应用身份变化，并恢复可本地使用的能力声明。

- 本分支 178 项 Python 回归：Mac 177 通过、1 项 Windows 专属跳过；路由检查通过。
- 隔离原生测试中，两份不同代码哈希的应用共享同一证书与身份要求。第二版在禁止授权弹窗时成功读取第一版创建的测试钥匙串项目；临时签名对照被拒绝。
- 实际应用两次重建生成不同 ASAR 摘要，10 个本机重签组件全部满足上一版的身份要求；麦克风、相机等声明保持，未修改的 CodexCLI 与 Sparkle 官方签名保留。
- 扩展版实际 Mac 副本已通过目录、思考档位、上下文、同聊天切换、大工具集及隔离启动六项检查。纯 DS 版实际 Mac 副本也已通过目录、思考档位、上下文、同聊天切换及隔离启动五项检查。验证请求使用本机模拟服务。
- 安装前后验证签名身份；已有证书丢失、被替换或与 baseline 不匹配会停止，避免自动换身份。
- 首次从旧临时签名迁移仍需按 macOS 提示确认所需权限；本次测试不调用用户麦克风或录屏，不修改 TCC 记录，不读取真实凭据。
- CI 增加独立 macOS 原生签名/钥匙串检查，Windows/Linux 回归和 Windows 无窗口后台检查继续运行。

---

# v0.4.2-proxy.1：Windows 自动更新修复

2026-10-19，将独立直连版已验证的两项 Windows 后台更新修复同步到扩展版自己的通道。

- Mac 170 项单元测试：169 通过、1 项 Windows 专属跳过；Node 路由与同聊天切换检查通过。
- Windows CI 增加真正的 `pythonw.exe` 全套回归，检查后台校验与控制台凭据 helper 的兼容性。
- 实际临时目录回归验证：进程从旧 runtime 启动时，先离开目录，再替换为扩展版 runtime，并保留旧副本。
- 调度任务回归覆盖原定义备份、只修改工作目录、保留禁用状态与触发器，以及未注册/已迁移时跳过。
- 扩展版专项回归确认：只有全部验证通过后才迁移旧任务；目录、思考、上下文、同聊天切换、大工具集与启动六项检查仍按原顺序执行。
- 模型路由、代理/本地适配和聊天压缩实现沿用 0.4.1；本次验证更新与后台执行路径，不提交真实模型推理回合。
- 两个分支各有发布清单与序号；本次只推进扩展版，Windows/Linux 各 Python 3.11/3.13 的结果见本次提交 Actions。

---

# v0.4.1-proxy.1：大量工具下的压缩循环

2026-10-19，修复 GitHub 旧扩展版默认关闭工具发现造成的 MCP 定义全量注入。故障聊天的窗口始终为 500,000；压缩后约 25K 的历史在下一次请求重新叠加工具开销，回到约 433K。此问题与先前的 128K 窗口回落分别验证。

- 166 项 Python 单元测试：Mac 165 通过、1 项 Windows 专属跳过；Node 路由和同聊天切换测试通过。
- 增加必须通过的真实内置 CLI 大工具集检查：160 个大定义 MCP 工具，7 个支持工具的模型分别完成三轮搜索/执行，在中间强制触发一次自动压缩，随后继续调用工具，窗口保持各自设置。初始请求体积有上限断言，工具执行由合成 MCP 服务实际响应。
- 隔离目录、逐档思考、上下文、同聊天切换、大工具集和应用启动六组安装检查通过；自动更新使用相同门槛。
- Grok 4.5、Grok 4.7、Gemini Flash、Opus Thinking 经现有代理完成真实的延迟工具搜索、加载及调用。Grok 4.7 另完成安装包凭据 helper 启动、短回复和临时文件读取。
- 使用故障聊天的只读副本、160 个合成 MCP 工具和 Grok 4.7 high 继续两轮：输入分别为 42,760 和 42,843 tokens，窗口均为 500,000，压缩次数为 0。原聊天文件前后摘要一致。
- 修复同时覆盖首次构建、模型规格同步、旧配置迁移，以及本地服务离线时保留旧规格的路径。用户的 8K / 32K 本地窗口和主动调小设置保留。
- 仅转换工具调用格式；搜索、执行和权限判断保留在原生客户端。压缩请求不会因为历史中包含工具搜索结果而重新启用工具。

---

# v0.4.0-proxy.1：本地模型、可选 DS 与同聊天切换

2026-10-18，扩展版保留独立发布通道。新增本地模型发现、可选 DeepSeek、统一验证命令和同聊天切换。

- Mac 单元测试 158 项：157 通过、1 项 Windows 专属跳过；Node 请求路由、缓存、忙碌保护、显式外部服务商与切换失败回退通过。
- 实际内置 CLI 验证已保存聊天从 128K 修复为 Grok 500K，强制触发一次真实压缩后仍为 500K；切换 DS、Gemini、原生 GPT 后保留同一聊天 ID、历史、审批、权限和工作目录。
- 现有 Ollama 0.40.2 的五个模型均返回真实响应：Ellaria 9B、Lumimaid 8B、Llama 3.1 Stheno 8B、Qwen3 8B 和 Qwen3.5 9B；两款 Qwen 另通过 shell 读取临时文件回合。只聊天模型按非空完成响应验证，不要求角色模型逐字服从测试标记。
- Qwen3.5 原模板拒绝出现在中途的系统消息；按需兼容进程合并前置说明后，回复与工具回合通过。原模型模板、权重与服务配置未修改；实际加载窗口 32,768，目录同时保留模型最大值 262,144。
- 完整扩展版 10 个自定义模型通过实际 CLI 目录、思考档位、上下文与同聊天切换；隔离应用启动通过。首次启动检查在 180 秒超时，采样定位到 macOS 系统证书读取等待；将等待上限调整为 360 秒后实测 346.62 秒启动，renderer / app-server 均保持运行。
- 不带 DS 的全新本地模型版本通过五项检查，随后在独立应用目标和临时 Codex home 中实际安装、恢复，原应用摘要及原配置复原；测试明确拒绝任何 DS 凭据访问。
- 目录和路由分别记录当前窗口与最大值；本地服务较小窗口可以持久保存，卸载模型或断线后不跳回最大值。连接地址变化计入更新摘要。
- 四款代理模型最低 / 最高思考档位的 8 次真实短响应通过；Gemini 与 Opus 的原生 CLI 同聊天切换低 / 高档也通过。
- Windows 的独立 DS 0.3.1 已正式安装，139 项实机单元测试及目录、思考、上下文、启动检查通过；自动更新与模型同步开启，官方 Store 来源未改变。扩展版的新增本地接入实测环境为 Mac → 已有 Windows Ollama 服务。

---

# v0.3.1：手动思考档位修复

2026-10-18，修复扩展模型仅注册 high、初始目录关闭思考切换的问题。已知型号的档位按厂商与现有代理适配器核对，带 -high / -thinking 的 ID 也支持逐回合调整。

- 自动同步及初次构建共用档位配置；未知后续型号从文档的 Supported 标题读取，单档模型默认档位保持有效。
- 新增真实内置 CLI 回归：读取 model/list 的可选档位，在同一聊天逐档切换并回到第一档，检查发往本机模拟接口的 reasoning.effort 完全一致。
- 安装与自动更新将思考档位测试加入必过门槛。
- Mac 150 项单元测试：149 通过、1 项 Windows 专属跳过；实际目录、17 个档位及回切、上下文和隔离启动通过。
- 现有代理的 8 次真实短响应通过：四个模型分别测试最低和最高档，均返回预期内容。

---

# v0.3.0：独立更新通道与模型同步

2026-10-18，新增补丁自身更新、原生 GPT 目录刷新、DS 官方规格读取及模型偏好持久化。扩展版同时加入代理模型发现与 Grok / Gemini Flash / Opus 官方规格同步。

- 本分支 148 项 Python 回归测试，Mac 147 项通过、1 项 Windows 专属测试跳过；Node 路由测试通过。
- 回归覆盖：通道隔离、提交与文件摘要、拒绝降级、隔离包测试、缓存篡改、安装后运行时代码激活重试、型号新增与下线、隐藏与主动调小窗口、未知规格等待及断网保留。
- 真实模型元数据刷新不发送推理回合。通过本机官方 `/models` 与厂商文档核对最大窗口，目录与路由共享同一规格快照。
- 扩展版真实 Mac 官方副本签名、ASAR、5 个新增模型的目录路由和两轮模拟上下文通过；压缩事件及额外压缩请求均为 0。
- 首次隔离启动超过 180 秒，未安装；单独复测 6.38 秒通过，renderer 与 app-server 保持存活。DS 版独立副本启动 92.46 秒通过。
- 两次真实模型规格刷新摘要一致、告警为空，未变化检查不触发重建。
- 各平台实机及 CI 结果分别记录；下方 v0.2.x 是历史正式安装记录。

---

# 0.2.1-proxy.2：修复上下文上限与频繁压缩

2026-10-18，修正扩展版四个模型统一为 128k 的错误。默认与最大窗口按官网规格设置：Grok 4.5 / 4.7 各 500,000，Gemini 3.8 Flash 为 1,048,576，Opus 4.6 Thinking 为 1,000,000 token。规格来源见 README。

- 目录取消 GPT 模板的 95% 窗口折减；新代理线程填入模型窗口和 90% 自动压缩阈值。显式较小的每线程配置保留，GPT / DS 全局设置保持原样。
- 实际修改后的客户端请求和预热方法通过 481 项行为断言；Python 单元测试共 126 项，Mac 125 项通过、1 项 Windows 专属测试跳过。
- 新增本机模拟回归：在全局配置仍为 128k 窗口、90k 压缩阈值时，实际内置 CLI 为每个模型连续处理两轮模拟 180k 占用。各模型回报官网窗口；压缩事件 0，额外压缩请求 0。该测试不连接模型服务商。
- 四个模型分别通过真实小型回复与读文件工具回合；实际 `modelContextWindow` 分别为 500,000 / 500,000 / 1,048,576 / 1,000,000。
- Opus 官网 1M 与当前 CLIProxyAPI 8.0.32 Antigravity 静态表 200k 的差异已注明；未执行百万 token 的上游压力请求。
- 兼容安装和自动监测新增上下文回归通过门槛，避免后续构建重新带入小窗口。
- 隔离应用冷启动实测耗时约 151 秒；启动探测等待上限从 60 秒调整为已验证的 180 秒，保留 renderer 与 app-server 持续存活条件。

---

# 现有代理扩展分支验证

2026-10-18，在 macOS 的现有 EasyCLIProxyAPI 接口上验证，应用基线 `26.1013.40934` / build `20770`。

| 模型 | Codex 实际回复 | shell 读取临时文件并回传 |
| --- | --- | --- |
| Grok 4.5 (`grok-4.5`) | 通过 | 通过 |
| Grok 4.7 (`grok-4.7`) | 通过 | 通过 |
| Gemini 3.8 Flash (`gemini-3.8-flash-high`) | 通过 | 通过 |
| Claude Opus 4.6 Thinking (`claude-opus-4-6-thinking`) | 通过 | 通过 |

- 使用实际内置 app-server、临时配置与临时工作目录，每个模型执行两个真实回合；流式事件、线程提供商和工具执行记录均已核对。
- 最终构建与联网测试使用相同的模型目录摘要和 CLI 文件摘要。
- 实际客户端的请求与预热路由共 415 项行为断言通过，GPT、DeepSeek、远端和显式自定义提供商均覆盖。
- 离线探测读取 13 个可见模型，验证 1 个 DS 与 4 个代理临时线程；推理调用为 0。
- 签名、ASAR 校验、完整应用语法与隔离 renderer / app-server 启动通过。
- 124 项 Python 单元测试中，Mac 123 项通过、1 项 Windows 专属测试跳过；Node 路由测试通过。
- 初次隔离启动使用了 DS 全局默认配置，未就绪；启动探测随后按正式安装行为保留原生默认提供商，通过后才允许安装。
- 本机接口当时没有提供 Opus 5.5 或 Gemini 4；不注册这些名称。Opus 使用已测试的 4.6 Thinking。

本机已正式安装并重新启动，应用签名与资源完整性通过复核。安装配置仅更新模型目录并新增代理服务商，原 GPT 默认与 DeepSeek 配置保留。自动监测已接续新构建，状态为 installed，周期为 300 秒。

该分支代理接入的实机记录为 macOS。Windows 部署与监测的既有验证见下方 v0.2.x 记录。

---

# v0.2.1 正式安装验证

2026-10-18，公司 Windows 电脑已完成正式安装、开始菜单入口创建及自动监测启用。新应用启动后保持运行，原始 Store ASAR 摘要保持不变。

- Windows 当前登录用户的凭据管理器写入和读取通过，实际提供商认证命令返回正确凭据。
- DeepSeek `/models` 返回 HTTP 200，并包含 `deepseek-flash`；该检查未发送推理回合。
- 自动监测已接续正式构建，状态为 installed，周期为 300 秒。
- Windows 实机 115 项单元测试全部通过；Mac 114 项通过、1 项 Windows 专属测试跳过。
- 修复：仅作为只读来源的官方应用，允许它的常驻 `resources/codex-windows-sandbox-service.exe` 继续运行；GUI、其他后台程序及将被替换的受管副本仍受退出检查保护。
- 修复：无窗口 Python 启动安装器时，认证子命令使用 `python.exe`，确保凭据可以通过标准输出返回。

远程登录会话的凭据管理器限制由当前用户的交互式安装任务处理。API key 经过加密连接与经过认证的本机命名管道送入系统凭据管理器，未写入脚本、任务参数或仓库。

---

# v0.2.0 验证记录

验证日期：2026-10-18。所有应用测试使用独立副本与临时配置；自动监测测试不调用付费模型。

| 平台 | 实际应用 |
| --- | --- |
| Mac | Apple Silicon；应用 `26.1013.40934`，build `20770`；CLI `0.163.0-alpha.10.2` |
| Windows | 公司电脑 Windows；MSIX `OpenAI.Codex 26.1013.4894.0`，内部应用 `26.1013.40934`，build `20770` |

| 检查 | 结果 |
| --- | --- |
| Python 单元测试 | 共 112 项：Windows 实机全部通过；Mac 111 项通过、1 项 Windows 专属长路径测试跳过 |
| 实际 Mac / Windows 客户端契约 | 两端各 227 项 Node VM 行为断言通过；完整修改文件语法通过 |
| Mac 构建与原生校验 | 官方签名、ASAR 成员、Framework 摘要、副本重签均通过 |
| Windows 构建与原生校验 | 官方发布者签名通过；21,073 个成员仅一个客户端内容变化；PE 资源摘要更新、ASAR 完整性开关保留 |
| 两端离线 app-server | Flash 目录恰好一个、临时线程提供商正确；推理回合 0 次 |
| 两端隔离启动 | 实际修改后的主进程、renderer 和 app-server 启动通过；测试进程随后清理 |
| Mac 安装 / 恢复 | 副本目标完成安装和恢复；恢复原始 ASAR 与配置不存在状态 |
| Windows 安装 / 恢复 | 独立目录完成实际安装和恢复；开始菜单快捷方式目标正确；恢复后移除本次副本、快捷方式和临时配置；官方 ASAR 摘要保持原值 |
| Mac 自动补装完整模拟 | 接续旧补丁 → 官方副本覆盖 → 稳定检查 → 构建及两项探测 → 自动安装；新 baseline 和模型目录持久化，额外用户设置保留 |
| 通知去重 | 自动补装模拟记录一次完成通知，后续未变化轮询保持安静；不兼容、失败、重试和运行中等待由单元测试覆盖 |
| Windows 系统调度 | 当前用户 SID / InteractiveToken 注册成功；禁用状态读取、无操作任务实际运行通过；测试任务已删除 |

Windows 实机验证同时发现并修复了深层资源的长路径复制问题。完整官方 Store 安装始终作为只读来源，测试不迁移个人 API key。安装事务测试用占位凭据可用性替身，实际 API 凭据读写不属于该测试。

Mac 本机监测已通过 launchd 注册和首次接续检查，状态为 installed；启用通知已提交系统通知接口。

v0.2 的联网 DeepSeek 回合没有重新执行；此前真实回复与工具调用结果保留在下方历史记录。发布物只包含源码与文档，应用副本、提取证据、模型模板和个人配置均保留在本地。

---

# v0.1.0 验证记录

验证日期：2026-10-18。环境：Apple Silicon macOS，官方应用 `26.1013.40934`（20770），内置 CLI `0.163.0-alpha.10.2`。

| 检查 | 结果 |
| --- | --- |
| Python 离线测试 | 15 项通过 |
| JavaScript 路由测试 | 通过：Flash 路由及 GPT、远端、显式自定义提供商、已有聊天保留 |
| 实际客户端方法 | 从构建产物提取真实请求方法执行，结果符合预期 |
| 官方应用副本构建 | 原始包哈希、代码锚点、ASAR 分块哈希均通过 |
| ASAR 成员比对 | 21,174 个成员，仅声明的一个客户端文件内容变化 |
| macOS 签名与摘要 | 副本深层签名、包头及 Framework 配套摘要通过 |
| 隔离启动 | 使用临时用户目录成功启动主进程和渲染进程，之后关闭测试进程 |
| DeepSeek 文字回复 | 临时聊天返回 `DS41F_OK` |
| DeepSeek 工具调用 | 使用命令执行工具读取临时文件并返回 `DS41F_TOOL_OK` |
| 独立目标安装/恢复 | 在单独复制的应用目标和临时 Codex home 中完成；恢复官方原始哈希和配置不存在状态 |
| 失败回滚 | 合成用例验证安装后校验失败时恢复应用与配置 |

公开版构建器、安装器和测试脚本均参与上述本地验证。本机正式应用未参与公开版安装/恢复测试，继续使用此前已安装的补丁。

GitHub Actions 执行跨平台离线测试，不执行 macOS 应用启动或 DeepSeek API 请求。实际联网调用必须由使用者显式运行 `scripts/test_connection.py … --run`。

发布物只包含源代码与文档。应用副本、日志、生成的完整模型目录、个人配置及钥匙串数据保留在本地。

## 0.5.11 / 0.4.11: macOS scheduled signature checks

The same read-only signature command was exercised under launchd Background and Standard process classes. Standard completed in approximately one second; Background stalled under I/O throttling. The monitor now uses Standard while retaining its bounded timer, quiet execution, and no KeepAlive. Already-loaded older macOS jobs restore normal scheduling for their own process and children at the start of each check, without stopping the job. Windows task identity, hidden execution and triggers are unchanged.


### Update identity, backend exit and edge interaction regressions

- Bind a candidate to one runtime/app identity across discovery, verification, manual start and activation. Rechecking the same candidate retains its start intent and prepared build. An unchanged verified installation must not redownload, offer an install, or raise another update notice.
- Keep discovery feedback separate from installation actions: download and compatibility checks show their current phase, while only a verified pending update gets a Start button. One explicit start continues through the stability delay; a ready installation skips new network discovery after app exit.
- Windows exit tests run copied disposable executables as an ordinary user. Verify that a headless process inside the exact application root exits, while another root and the shared sandbox service survive. Never run termination tests against an active user app.
- Both native settings hosts must exercise edge click ordering: reveal first, directional button entry next, rapid clicks coalesced, and pending entry cancelled on hide/detach. Windows controls retain keyboard focus styling without the default dotted adorner.
- Both native edition probes carry usage above the destination compaction threshold before a GPT quota failure, then switch within the same chat, compact through the destination model, execute tools, and verify no further GPT request. The original reported user's OS/version/error should be recorded separately when available.

## Issue #4: renderer quota lock before inference

The previous CLI fixtures bypassed the desktop send button. They therefore missed the account-wide local composer lock reported in Issue #4. That lock is now scoped at two renderer expressions to the exact external route IDs in the validated installation; the global account state and other submit conditions are unchanged.

The release gate additionally requires `test_native_composer_quota_button_before_inference` and `test_composer_quota_model_switch_keeps_gpt_and_other_submit_guards`. Run the former deliberately with `CODEX_COMPOSER_APP`, `CODEX_COMPOSER_FIXTURE` (a fresh private fixture directory), and `CODEX_COMPOSER_EDITION=ds` or `proxy`; routine user preflight does not open a desktop UI. Windows runs under the ordinary interactive user in the registered package context. Test both editions on both systems, retaining source/adapted hashes and logs.

This source-bound renderer test extracts the shipped account-quota predicate and complete composer submit expression, supplies synthetic account/model state, and checks real DOM button click dispatch in an isolated original renderer. It covers five-hour/weekly exhaustion, new/existing state, GPT → external → GPT, unknown model IDs, and every remaining native submit guard. The actual original renderer must also load both verified replacement assets through the inherited pipe. Its receipt explicitly identifies this as a synthetic state/DOM contract, not a complete signed-in composer session. A reporter's original naturally exhausted account remains a separate acceptance check.

Additional launch checks: macOS publisher/signature and ASAR stay intact, and the signed app's responsible PID equals its own PID so permissions remain attributed to the app rather than the helper; Windows retains package identity without forcing all descendant tools into that identity; no debugging TCP port or console window is opened. Compatibility errors release paused renderers and leave the app running. Starting from the original icon does not claim compatibility: instructions must direct users to the settings Launch Codex entry for this runtime-only layer.
