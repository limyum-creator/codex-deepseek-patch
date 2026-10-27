# Official-client architecture

## Ownership

`official-client-v1` uses the vendor-signed desktop app and bundled CLI without modifying either. macOS keeps the official signing identity. Windows starts the registered Store package, preserving package identity and the vendor updater. A first migration can restore the signed original Mac backup; subsequent installs never write app bytes.

The adapter owns a scoped native provider configuration, model catalog, small Python runtime, per-user service and update state. It does not own the original authentication store, tool runtime, permissions, personal history database, external model service or its subscription login.

## Request path

```text
Official desktop and bundled CLI
  └─ Native model catalog and native OpenAI-authenticated provider
      └─ Authenticated loopback adapter (HTTP / WebSocket)
          ├─ GPT → original OpenAI destination and original auth
          ├─ Registered Responses service → its own credential
          └─ Registered Chat Completions service → protocol bridge + its own credential
```

The built-in `openai` provider identity and official authentication remain intact. This preserves native composer prediction eligibility. GPT inference and its compaction protocol pass through to the official destination. External native compaction triggers use only the selected service to generate a readable handoff. The native wire carrier has an explicit adapter namespace; it is not OpenAI ciphertext and is expanded before either GPT or external inference. Codex continues to schedule compaction, install history, and apply each model's window. HTTP request compression follows native defaults and the user's choices. An unchanged GPT request keeps its exact compressed wire bytes; external routing decodes it and uses only the selected service's credential. Python 3.14 uses stdlib zstd; earlier Python uses the untouched Codex-bundled Node codec over pipes, without another installed runtime or resident process. Legacy patch-owned compression overrides are removed during migration. WebSocket per-message compression is negotiated independently with stdlib zlib, including context takeover and fragmented messages; existing ping/pong control frames are relayed. GPT network handshakes preserve routing/sticky headers; external requests never receive the OpenAI credential.

HTTP CONNECT honors the existing system proxy for external destinations, while local destinations bypass it. HTTP redirects are not followed with credentials. The listener binds numeric loopback only, uses a private token path and validates its installation identity. User-supplied LAN endpoints are explicit connections, not network discovery.

## Model capabilities and tools

Native catalog entries record each model's maximum/effective context, compaction limit, input modalities and reasoning levels. User reductions live separately from provider specifications; sync retains reductions and hidden selections. GPT retains the official effective-window percentage and automatic compaction policy; only an explicit user threshold overrides it. External models default to 90% compaction and accept a user threshold up to their configured window. Unsupported/unknown specifications are reported instead of replaced with a 128K default.

Responses interfaces can preserve native functions. Where required, `responses_compat.py` translates namespace/custom/tool-search items to compatible functions and restores native result shapes. Tool search remains lazy, and Codex performs execution and approval. `additional_tools` declarations are promoted to the applicable current tool definitions, not inserted as chat text.

`chat_completions_compat.py` translates standard messages, images, calls/results, parallel tool chunks, streaming lifecycle and usage. Reasoning effort is forwarded only when explicitly enabled for that interface. Unsupported modalities produce a capability error. The same module also adapts native Anthropic Messages: system blocks, image sources, tool use/results, SSE block lifecycles, cache-aware usage and same-model thinking signatures. It pins API version 2023-06-01 and uses x-api-key independently of native GPT authentication. Model metadata or explicit operator specs supply max_output_tokens and reasoning capabilities; no guessed output budget is added.

Models with declared tool support retain the original CLI tool capabilities. Chat-only models omit tools. Native services continue to control whether individual tools are available in a particular session.

## Same-chat switching and compression

All registered models use the same native provider identity, so switching does not unload and reconstruct a chat for each vendor. Original chat ID, working directory and approval/sandbox settings stay in the native session. Native catalog metadata applies the selected model's context and per-turn reasoning effort.

External reasoning items receive model provenance. A continuation on the same model restores its exact provider-specific state. A different model receives readable text and tool history, without another model's signature or incompatible reasoning-item fields. This rule uses model identity and protocol shapes, not a hardcoded list of vendors.

Older external signatures are identified by a bounded, read-only rollout scan: `turn_context.model` associates known external models with SHA-256 signature hashes. The private index stores hash → model, never ciphertext or chat content. Ambiguous origins are excluded.

Old GPT compaction blocks are handled lazily without calling GPT. The native request identifies its thread. A read-only standard-library loader verifies that chat's JSONL identity, ordinal continuity, exact compaction item and any bounded shared-history parent prefixes. It reads visible messages and tool results only; opaque reasoning and media bytes are excluded. No database writes, whole-library content index, decryption, or new resident service is involved.

The selected route summarizes the verified text through its own Responses or Chat Completions endpoint. Recovery uses the declared effective model context, bounded UTF-8 chunks and hierarchical reduction; it does not alter the native context window. Summary requests have no tools. An account/thread/model/endpoint/window-scoped private cache avoids repeat calls, and old official handoff caches are read locally and reduced if the chosen window is smaller.

Missing prefixes, ambiguous identity, invalid ordinal/byte boundaries, malformed records, unresolved rollback records and unsupported storage formats return an explicit recovery error before normal inference continues. The current reader supports uncompressed JSONL and native paginated fork prefixes. Oversized, nonreducing or interrupted summaries are not cached as complete. New external handoffs are self-contained in native history and do not depend on the recovery cache. Old-history recovery publishes bounded progress metadata and a cancellation marker; no messages, tool contents or credentials appear in those status records.


## Installation and recovery

Staging creates only runtime/config artifacts and immutable digests. Validation checks that staged bytes match the source tested by the background native CLI suite. Installation checks the official publisher, unchanged source, runtime/profile/catalog digests and the exact service command/health identity.

Under a state lock, activation re-reads current settings and changes only owned fields. Existing chats migrate through native metadata RPC, preserving model, effort, archive state, content and recency. No SQL writes or history copies occur. Archived chats use explicit unarchive/resume/rearchive phases; recovery records the actual phase because unarchive is not idempotent.

A durable activation journal covers app restoration, scoped configuration, hash index, metadata migration and service state. Each restoration is attempted independently so a service failure cannot skip app/config recovery. A failed or interrupted activation can recover and retry. Restore follows the ownership chain to the pre-adapter configuration, migrates newer adapter chats to the earlier default provider, and retains the official app.

Services use launchd on macOS and least-privilege Task Scheduler with pythonw on Windows. Windows task XML has matching UTF-16 declaration/encoding; numeric COM states distinguish Ready, Disabled and Running. Existing task state is restored precisely.

## Updates and source distribution

DS `main` and extension `codex/existing-proxy-models` have separate versions, increasing feed sequences and immutable source commits. Downloads validate channel, inventory, hashes and isolated tests before activation. A native install rejects a legacy-architecture downgrade.

The monitor is scheduled and exits after each check. It compares app/runtime/model identities and waits for normal app exit. It does not send inference requests. The adapter is a separate small per-user process, with no database or model engine. The native settings host starts on demand. Hiding preserves its lightweight shortcut listener; explicitly quitting releases it and exits.

The repository retains legacy source and tests for recovery and historical compatibility. New builds do not edit ASAR, sign binaries, copy a Windows app or use legacy GUI boot probes. Only source/documentation is published; catalogs containing vendor prompts, app bundles, user credentials and private test evidence remain local.

## Non-blocking update progress

The monitor publishes a small private progress document containing fixed display fields and measured work counts. Unknown-duration phases use an indeterminate bar. The shared AppKit/WPF settings host renders progress and issues only allowlisted actions bound to the current update ID; state JSON never supplies a command to execute.

Dismissing the window does not cancel the worker. Starting an update records the user's request; Codex is closed only after preparation and validation. Completion offers a launch button and does not reopen Codex automatically. Background notices signal the existing singleton host, or start it without activation, and reveal only a droplet badge. Explicit navigation opens the integrated Updates card. The host reads small progress documents directly, without polling Python child processes or a UI server.


## Integrated settings and release gates

`management.py` owns an allowlisted action controller and native AppKit/WPF views. Each view has a per-state-directory singleton lock, asynchronous private request/result files, cancellation, and optimistic revision checks. Credentials are stored in the existing OS credential store or private owned key files; connection documents contain references. Exported diagnosis contains only allowlisted status fields. A separate per-user launcher leaves the original Codex icon and application untouched.

Both editions are built from a single committed core using isolated Git indexes. Source and portable artifact identities remain separate. Promotion requires four receipts (macOS/Windows × DS/extension), exact artifact/core hashes and successful candidate CI. The two feed versions/sequences remain independent and are pushed atomically. Old updaters continue to receive their historical portable paths and a bounded file inventory. New clients prefer an immutable archive with exact per-file verification; integrity failures never fall back to weaker validation.

Explicit checks use a coalesced request under a separate lock and are consumed only by the active monitor owner. Idle maintenance prunes only owned, unreferenced expired artifacts, preserving active/pending/recovery chains and recent rollback versions. No new scheduled or permanent process is added.

Native HTTP services below the exact `/backend-api/codex/` prefix pass through to the fixed official origin with their original method, query, authentication, encoded body and response. This includes search, image generation/editing and future native HTTP services, without a per-tool endpoint list. Only Responses inference and compaction enter model routing. Other origins, path traversal and unsupported non-Responses WebSocket upgrades are rejected. Dot's durable WebSocket belongs to the official app's own networking stack; system DNS/proxy conflicts are diagnosed independently from inference routing.

The inference compatibility boundary is the native model catalog/configuration and wire protocols. New native HTTP tool paths and changing official model specifications do not require their own adapter rules. The monitor checks compatibility after app updates; protocol or catalog changes can still require a patch release. Settings and the updater own only patch state and never replace the vendor updater.

## Independent-model composer quota

The native desktop's local submit guard can block all model selections when ChatGPT account usage is exhausted, before any inference reaches the adapter. The settings **Launch Codex** action starts the unchanged signed executable with an inherited private debugging pipe. A small controller intercepts exactly the two integrity-checked composer assets in memory: it scopes the account-quota submit guard and its composer banner to models absent from the installation's explicit external route list. The native quota state and every other submit condition remain untouched. No debugging TCP listener, copied application, modified ASAR or re-signing is involved.

The source layout and original/adapted hashes are bound at build time and rechecked at installation and launch. Unknown layouts stop preparation. Windows launches the controller in the registered package context and applies package identity to the official child while retaining the normal descendant breakaway policy. The controller stays alive for this app session; intercepted errors disable interception and keep the pipe open until normal app exit, because closing Chromium's pipe itself exits the browser.

This compatibility starts only through the patch settings/update completion entry. Opening the original icon from a fully stopped state uses the unadapted native UI; the user must normally quit and launch through settings to enable this session's independent-model sending. It does not attach to or restart an existing app session. The full account state remains available to GPT, and the original services continue to enforce GPT quotas.

Official WebSocket close codes/reasons reach the original client, which owns retry/fallback decisions. Adapter-generated WebSocket errors include the native HTTP status field; an unrecognized error must not become a silent wait until the native idle deadline. Large messages use standard fragments so control frames can pass during upload. The adapter neither removes original images to fit an upstream limit nor increases the native response deadline.

## Transport and resource limits

The authenticated native request path has no patch-specific payload ceiling: both HTTP inference/compaction and WebSocket history can exceed 64 MB. Official response bytes, including upstream rejection status and WebSocket close reason, pass through; the native client controls retries and inference idle deadlines. Registered-model inference also releases its socket read deadline after connecting; cancellation closes the upstream socket.

Retained bounds belong to specific adapter operations, rather than model context: external Responses events/nonstream replies have a 64 MB parser bound, Chat Completions events 8 MiB, legacy origin indexes 32 MiB, and readable-history recovery scans 512 MiB with 64 MiB per record/visible transcript. Recovery summaries have bounded output and reduction passes to terminate nonreducing loops. These limits return explicit errors and do not truncate native GPT history. The compatibility image tool delegates prompt, reference count, image size and successful response-size acceptance to the configured service; its existing 600-second request deadline and 900-second MCP deadline remain specific to that optional tool. Native Codex image generation does not use those deadlines.

A limit change requires a named scope, protocol/provider evidence or a resource-budget rationale, and a regression that includes a legitimate large input. No global native feature flag may be changed merely to simplify the adapter implementation.

## Candidate onboarding and diagnosis

The shared settings controller opens a first-install card when its own settings file does not exist. It keeps a private edition-bound draft, checks the signed app and model metadata, builds and runs the existing isolated verification, then offers explicit installation. A running app or independently changed native configuration blocks activation. Existing OpenAI base URL/provider ownership is detected before preparation; users disconnect their previous manager through that manager. Installation reuses the native transaction; monitor registration can resume after a committed install without repeating it. A graphical downloadable bootstrap/runtime bundle remains separate packaging work; the current candidate entry still requires Python 3.11+ and `patch.py settings`.

The adapter retains at most 64 request metadata records in memory. Completed records disappear from the diagnostic view after 30 minutes and all records disappear on service restart. The authenticated local diagnostics endpoint reports model, service host/port, protocol, phase, elapsed time and status category; bodies, keys, private URL paths and raw exceptions are excluded. The settings card reads this data on demand. Exported reports keep their existing allowlist.

Specifications carry field-level provenance and the originally reported window. Operator/service conflicts are shown alongside the effective value. The metadata does not alter request shaping, context precedence, reductions or hidden-model selection.

## Optional first-install bundles

The release builder can package a verified portable source with a SHA256-pinned Python runtime, independent for each edition and CPU. These are optional release assets, outside the 100-file source update feed. The original Codex application is never bundled. The bootstrap copies verified files atomically to an edition-specific, content-addressed user directory, then opens the existing setup card with that stable interpreter. It installs no system Python and changes no native configuration until the setup transaction is explicitly invoked. macOS public distribution requires signing/notarizing the installer itself; Windows uses a windowless WScript launcher and includes a manual fallback for hosts where WScript is disabled. Runtime licenses remain in the package.

First-setup progress travels through the existing private job files. Build and verification phases are fixed vocabulary; compatibility checks report real completed/total counts. Cancellation terminates only the owned child process group. No extra daemon, HTTP service or web UI is introduced.

## Unified release and migration

From 0.9.0, one canonical product feed lives at main/unified-update.json. Existing main/update.json (DS) and the old extension branch/update.json serve immutable compatibility bridges. The bridges contain the same core with metadata accepted by old updaters. They immediately expose the unified feature set and read the canonical feed afterwards. Exact same-version runtime equivalence avoids another restart just to change metadata. Future updates use the canonical package.

Persisted ds/proxy identities remain service/profile ownership affinities, preserving directory paths, credentials, monitor preferences and transactional rollback. They no longer gate model features. A unified runtime accepts known legacy affinities; a legacy runtime cannot claim another affinity. Only one public release is built after this transition.

Message identity uses a versioned msg_cma1_ origin envelope across native history and streaming item references. Same-model replay restores the provider ID; foreign full messages omit that private ID. Legacy complete UUIDv4 message items can be replayed inline on official routes without a reasoning stamp. Function/tool call_id linkage and original message content remain intact.
