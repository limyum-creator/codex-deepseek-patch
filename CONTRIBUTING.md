# Contributing

Issues and pull requests for app compatibility or platform support are welcome. Include OS/CPU, app version/build, bundled CLI version, the failing command, and a redacted error. For monitor issues, include its phase and the relevant redacted event.

Do not upload app bundles, complete extracted JavaScript, generated catalogs, user configuration, API keys, authentication files, or raw personal logs. Local evidence and backups belong in ignored build directories.

## 存储与清理规范 / Artifact storage and retention

项目源码以当前 Git 工作区为唯一入口：代码在 `src/`，测试在 `src/tests/`，文档在 `docs/`。历史源码由 Git 提交、分支及发布标签保存，不为每次修改复制一套源码。开发、验证与回滚材料统一放在项目内已忽略的 `.local/`，不再散放到用户配置目录、下载目录或其他项目。

| 目录 | 用途与保留规则 |
| --- | --- |
| `.local/releases/` | 最新及上一版的 DS/扩展版发布包、候选清单、两系统及旧版迁移验证记录、日志和校验值；每组保持可独立核验 |
| `.local/backups/` | 修改前配置、最后可用的恢复材料、迁移记录和签名恢复材料；按日期与用途命名，记录原位置及 SHA256 |
| `.local/records/` | 当前问题的最小复现、结果摘要、清理清单；已结束的历史调查压成一个归档，不保留重复工作目录 |
| `.local/assets/` | 项目独有的品牌源素材、参考与宣传成品；不作为测试缓存删除 |
| `.local/integrations/` | 尚未纳入公开源码的辅助集成源码及测试；正在使用的已安装入口保持原位 |
| `.local/cache/`、`.local/tmp/` | 可再生成的缓存与单次任务目录；每个任务单独命名，不保存唯一源码或唯一备份 |

正式安装仍使用平台要求的用户服务与配置位置；这些位置只容纳当前部署及直接回滚所需内容，不作开发仓库或历史构建仓库。清理开发材料不得改动 Magpie、其他已安装集成、Codex 的登录与聊天，也不得顺带操作未授权的远端设备。

清理时机与检查顺序：

1. **每次测试或调查结束**：确认进程和句柄已退出，保存必要结果后删除临时应用副本、浏览器测试配置、解包的第三方代码、下载包和编译缓存。临时目录使用自动回收的上下文；需要显式路径时放入 `.local/tmp/`，Python 缓存可通过 `PYTHONPYCACHEPREFIX` 指向 `.local/cache/pycache/`。
2. **一次发布完成后**：两系统及旧版迁移验证、源码 CI、公开下载校验均通过，再清理候选重复目录及临时工作区。保留最新与上一组发布包及其完整验证记录，不因清缓存删除验证凭据或尚未结束的候选。
3. **问题尚未解决时**：保留最小复现、故障版本、协议错误分类及必要日志。包含原始聊天、图片或凭据的临时载荷在取得脱敏结论后立即删除；普通临时日志保留 7 天，已解决问题的诊断日志最多保留 30 天，结论归档后删除大体积现场。
4. **每周及目录超过 1 GB 时**：盘点 `.local/` 的占用、过期缓存与备份重复。先保护当前部署、正在安装/回滚的引用、未解决问题和唯一素材；再删除无引用的可再生成内容。容量阈值用于触发检查，不直接触发删除。
5. **移除 Git 工作区时**：先核对 `git worktree list`、未提交改动和提交可达性；保留需要的提交引用后使用 `git worktree remove`。有改动的工作区先保存差异，禁止直接递归删除已注册工作区或执行无范围的 `git clean -fdx`。
6. **搬动和删除前后**：先记录来源、目标、大小、必要备份和活跃引用；复制后核对哈希，验证备份可读，再删旧位置。对正在使用的配置和可执行入口做前后校验，记录实际移除量及保留项。

私有备份目录仅限当前用户访问；不要在公开文档、提交或发布包中加入真实凭据、用户绝对路径、完整应用或聊天。签名身份、唯一恢复材料及用户明确保留的内容不按缓存期限自动删除。规范本身不安装定时清理程序；清理在已授权的任务内执行并留记录。公开包继续遵守既有文件清单与 100 文件兼容预算，优先更新现有规范文档。

## 统一版与旧用户迁移验证

从 0.9.0 起只维护一个产品与版本，在 macOS、Windows 两个平台验证。每个共享 bug 同时检查两个系统；旧 DS 和扩展版作为升级来源场景保留，不再作为独立功能版本。

- 统一更新源是 `main/unified-update.json`。两个旧 `update.json` 永久保留同一核心的一次性迁移包，后续无需复制功能发布。
- 旧安装的服务标识、目录与 profile edition 是安装归属信息。统一运行时接受已知旧标识；旧 DS 与 proxy 相互冒用、未知身份或降级到旧运行时继续拒绝。
- 保留用户密钥引用、连接、模型选择、隐藏项、上下文与压缩调小值、维护偏好和恢复链。相同版本且全部运行时文件相符的桥接包可直接沿用，避免重复安装。
- 单元和事务测试覆盖两个旧来源、升级失败回退、统一后下一次更新，以及首次安装。系统验证使用隔离环境；发布和现用机器安装分别记录。

## Native integration changes

1. Verify the original app publisher and bundled CLI before probing. New architecture must not modify ASAR, re-sign the app or copy a Store package.
2. Keep adapter rules protocol/capability based. Add synthetic coverage for arbitrary model identities, all supported wire APIs, streaming, tool results, errors, cancellation, reasoning provenance and context transitions.
3. Run the original CLI with isolated state and synthetic endpoints in the background. Do not launch GUI windows or touch the user's keychain during routine validation.
4. Preserve native login, tool execution, approvals, chat identity, working directory and readable-summary compaction. Test native API metadata migrations and interruption recovery; never write the history database directly.
5. Stage and validate the same bytes. Recheck source/runtime/profile identity before activation. Test rollback with fault injection and real Windows normal-user service states.
6. The Issue #4 composer compatibility is an explicit exception for in-memory UI adaptation, not for application-file edits. Only the selected independent model may bypass the ChatGPT account quota guard. Keep the account quota atom and every other submit guard unchanged. Use an inherited private pipe, verify exact source/adapted asset hashes, preserve the signed Mac app as its own permission-responsible process, and preserve Windows package identity with normal descendant-process behavior. Never turn a UI compatibility error into termination of a working app.
7. Quota regressions must cover the renderer's disabled button before any inference request, separately from CLI/provider 429 tests. Record synthetic DOM/selected-state coverage separately from a complete signed-in composer run; test five-hour/weekly limits, new/existing chat state, external/GPT switching, unknown models and other disabled conditions in both editions on both OSes.

## Monitor and configuration changes

Keep unchanged checks quiet, notifications deduplicated, and state writes atomic. Passive checks wait for app exit. An explicit Start update click may close the verified application and its remaining same-session backends; never terminate by executable name or affect another installation or shared sandbox service. Scheduled checks must not submit inference. Code updates follow the edition-pinned feed with digests and isolated preflight; reject native-to-legacy downgrades. Registration remains reversible.

Only owned configuration changes are allowed. Rebase against the latest configuration under the installation lock, retain unrelated edits, and restore the immediately previous state after activation failures. Preserve explicit model reductions/hiding and original server configuration. Cloud specification errors retain the last known good value.

## Validation

```sh
cd src
python3 -m unittest discover -s tests -v
node tests/route.test.cjs  # retained legacy regression
python3 patch.py build --app "/path/to/official/app" --connections "/path/to/connections.json" --output "/new/build/directory" --verify
```

On Windows use `python` and Windows paths. Validate unified on Mac/Windows, including both legacy upgrade sources. Record unit fixtures, original CLI synthetic integration, real model calls, service tests and actual installation separately. Models without credentials receive synthetic protocol coverage, not a real-model success label.

Live provider tests require user authorization and use minimal synthetic chats. Never publish app bundles, generated vendor catalogs, tokens, raw user history or private execution evidence. Real tests should use ephemeral authentication and avoid login refresh. Native verification does not require GUI boot or local signing tests; retain those only for legacy recovery changes.

## Source layout and portable releases

Development code lives in `src/`. Keep the root command wrappers small; all internal imports remain within the source tree. Tests and build fixtures are source materials, not additional running services.

Prepare the single product from one committed source revision, then validate its exact portable bytes on both platforms:

```sh
python3 src/scripts/release_manifest.py --commit FULL_SOURCE_SHA --prepare-unified PRIVATE_CANDIDATE_DIR --version VERSION
python3 src/scripts/release_manifest.py --verify-candidate PRIVATE_CANDIDATE_DIR/candidate.json --edition unified --app OFFICIAL_APP --output PRIVATE_RECEIPTS_DIR
```

Run verification on macOS and ordinary-user Windows `pythonw` without a console. Collect both receipts and their unit/native/prepared-build/coverage logs. Migration regressions cover both legacy affinities. The first unified candidate additionally binds both legacy bridge packages to the exact same shared-core digest; later candidates leave those immutable bridge feeds alone. Portable inventories retain historical paths and the 100-file limit for older updaters.

Push the source branch for CI. After source CI and both platform receipts pass:

```sh
python3 src/scripts/release_manifest.py --promote PRIVATE_CANDIDATE_DIR/candidate.json --receipts PRIVATE_RECEIPTS_DIR --execute
```

Promotion atomically publishes the unified feed/tag and, on the first transition only, the compatibility tags/legacy feeds. Every required regression must pass; protocol mocks, native synthetic services and real-account acceptance remain separately identified. Live inference is not part of ordinary release validation.

### 更新事务与空闲安装

更新检查、预下载和校验不退出 Codex。一个候选只保存一份进度与操作意图；用户点击开始后固定候选，准备完成再等待实际请求与工具结果结束，连续空闲 10 秒后关闭已核验的应用进程并安装。等待可取消，超过一小时自动结束本次等待并保留下载包；旧适配服务没有空闲协议时，等待用户正常退出，不据此推断空闲。关闭页面不取消更新。

下载进度按实际字节计量，文件校验和兼容检查按已完成项计量；阶段内进度不倒退。等待空闲、退出和启动没有可预知总量，显示状态与耗时。不得用计时器填充百分比，也不得让旧检查任务覆盖新安装状态。

安装事务必须在提交前验证新服务身份及更新运行时启动身份。故障恢复上一版运行时、服务和受管配置；恢复失败保留日志和备份，不显示成功。原版 Codex 的启动按钮是独立操作，确认目标主进程与服务就绪后才消耗点击；启动失败恢复按钮。关闭进程测试只使用临时目录中的替身，禁止对用户工作中的应用执行退出测试。

完整单元、原生 UI 与故障注入测试在发布两系统及旧版迁移门禁中执行；用户端下载仅做文件校验、全源码语法检查和轻量协议检查，随后用其原版 CLI 验证兼容性。下载检查不得重复启动整套 GUI/服务生命周期测试。

后台准备的新包使用 `prepared_idle` 状态：旧控制器只能识别 `prepared`，因此不得越过新空闲策略自行关闭应用。新安装器同时接受两种已校验状态，提交后统一为 `installed`。两系统及旧版迁移必须验证这条首次升级路径。

### Optional first-install assets

From a verified portable package (including its private `.package.json` receipt), build a disposable installer candidate:

```sh
PYTHONPATH=src python3 src/scripts/release_manifest.py --bootstrap-source VERIFIED_PORTABLE_DIRECTORY --runtime-archive PINNED_VENDOR_ARCHIVE --target macos-arm64 --output NEW_PRIVATE_DIRECTORY
```

Targets: macos-arm64, macos-x64, windows-x64, windows-arm64. Runtime pins live in the release builder. Retain vendor license files. Keep generated installers outside source feeds; validate their interpreter, native UI, stable copied path, edition boundaries and source integrity independently. Mac installer signatures/notarization belong to our installer only, never the official Codex app. Source promotion does not implicitly publish installer assets.
