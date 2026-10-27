"""Move existing chat provider metadata using the official app-server API only.

No turns, compaction, SQL writes, prompt changes or history copying. The caller
must have stopped the desktop before running this one-time migration. A durable
journal permits reversal and restores archived chats even after interruption.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from monitor import atomic_json
from official_client_config import PROVIDER


class MetadataRpc:
    """A bounded native RPC client which cannot submit a model turn."""
    METHODS = {"initialize", "thread/list", "thread/read", "thread/resume", "thread/unsubscribe",
               "thread/archive", "thread/unarchive"}

    def __init__(self, process):
        self.process, self.counter, self.events = process, 0, queue.Queue()
        def reader():
            for line in process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    pass
            self.events.put(None)
        threading.Thread(target=reader, daemon=True).start()

    def request(self, method, params):
        if method not in self.METHODS:
            raise ValueError("This migration client only permits metadata operations")
        self.counter += 1
        self.process.stdin.write(json.dumps({"id":self.counter, "method":method, "params":params}) + "\n")
        self.process.stdin.flush()
        until = time.monotonic() + 120
        while time.monotonic() < until:
            response = self.events.get(timeout=max(.1, until-time.monotonic()))
            if response is None:
                raise RuntimeError("The native metadata process stopped")
            if response.get("id") != self.counter:
                continue
            if "error" in response:
                raise RuntimeError("Native metadata operation failed: " + method + " (" +
                                   str(response["error"].get("code")) + ")")
            return response["result"]
        raise TimeoutError("Native metadata operation timed out")


@contextmanager
def native_session(app, codex_home):
    """Use official code with network, credentials, hooks and tool launch disabled."""
    import patch as p
    home = Path(codex_home)
    current = p.tomllib.loads(p.read_config(home / "config.toml"))
    args = [str(p.cli_path(app))]
    overrides = {"cli_auth_credentials_store":"ephemeral", "features.hooks":False,
                 "features.apps":False, "features.memories":False, "features.plugins":False,
                 "features.remote_plugin":False, "openai_base_url":"http://127.0.0.1:9/backend-api/codex"}
    # Do not create an incomplete provider table while inspecting a legacy home.
    # Native configuration validation requires a provider name even when unused.
    if PROVIDER in current.get("model_providers", {}):
        overrides["model_providers." + PROVIDER + ".base_url"] = "http://127.0.0.1:9/backend-api/codex"
    for name in current.get("mcp_servers", {}):
        # Native -c keys are dot-separated strings, not TOML key expressions.
        # Quoting a hyphenated name creates a new, invalid server entry.
        if "." in name:
            raise ValueError("Native metadata isolation cannot override an MCP name containing a dot")
        overrides["mcp_servers." + name + ".enabled"] = False
    for key, value in overrides.items():
        args += ["-c", key + "=" + json.dumps(value)]
    args += ["app-server", "--listen", "stdio://"]
    env = {**os.environ, "CODEX_HOME":str(home), "OPENAI_API_KEY":"native-metadata-only-placeholder",
           "HTTP_PROXY":"http://127.0.0.1:9", "HTTPS_PROXY":"http://127.0.0.1:9", "ALL_PROXY":"http://127.0.0.1:9",
           "NO_PROXY":"127.0.0.1,localhost", "no_proxy":"127.0.0.1,localhost"}
    options = p.background_options() if os.name == "nt" else {"start_new_session":True}
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", env=env, cwd=home, **options)
    try:
        rpc = MetadataRpc(process)
        rpc.request("initialize", {"clientInfo":{"name":"native_model_metadata_migration", "version":"1"},
                                   "capabilities":{"experimentalApi":True}})
        yield rpc
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=20, **p.background_options())
            else:
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def _metadata(thread):
    # updatedAt is a native maintenance timestamp and changes on resume/archive.
    # recencyAt controls chat recency and must be preserved alongside identity.
    keys = ("id", "cwd", "name", "title", "createdAt", "recencyAt", "projectId", "sectionId", "model", "reasoningEffort")
    return {key:thread.get(key) for key in keys if key in thread}


def _raw_digest(thread):
    return hashlib.sha256(json.dumps(_metadata(thread), sort_keys=True).encode()).hexdigest()


def _digest(thread):
    value = _metadata(thread)
    cwd = value.get("cwd")
    if isinstance(cwd, str):
        normalized = cwd.replace("/", "\\")
        if normalized.startswith("\\\\?\\UNC\\"):
            normalized = "\\\\" + normalized[8:]
        elif normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        drive, tail = ntpath.splitdrive(normalized)
        if drive and ntpath.isabs(normalized):
            # Native Windows APIs normalize duplicate separators and verbatim
            # prefixes. Keep directory case: case-sensitive folders can exist.
            if len(drive) == 2 and drive[1] == ":":
                normalized = drive.upper() + tail
            value["cwd"] = ntpath.normpath(normalized)
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def metadata_matches(before, after, *, resolved_defaults=None):
    """Keep explicit choices exact; an unset choice may resolve on native resume.

    Native thread/resume materializes model/effort defaults even without a turn.
    Its API cannot clear those fields back to null. Do not mistake that normal
    resolution for a workspace/name/recency change, or rewrite the saved source.
    """
    expected, actual = _metadata(before), _metadata(after)
    for key in ('model', 'reasoningEffort'):
        if expected.get(key) is not None:
            continue
        value = actual.get(key)
        if value is not None:
            if (not isinstance(value, str) or not value or
                (key == 'reasoningEffort' and value not in ('none','minimal','low','medium','high','xhigh','max','ultra'))):
                return False
            if resolved_defaults is not None and value != resolved_defaults.get(key):
                return False
        if key in expected:
            actual[key] = expected[key]
        else:
            actual.pop(key, None)
    return _digest(expected) == _digest(actual)


def inventory(rpc, providers):
    """Do not let a provider mutation change the pagination of the running list."""
    found = []
    for archived in (False, True):
        cursor, seen = None, set()
        while True:
            params = {"archived":archived, "limit":100, "modelProviders":sorted(providers), "useStateDbOnly":True}
            if cursor:
                params["cursor"] = cursor
            reply = rpc.request("thread/list", params)
            for thread in reply["data"]:
                if thread.get("modelProvider") in providers:
                    found.append({"id":thread["id"], "provider":thread["modelProvider"], "archived":archived,
                                  "path":thread.get("path"),
                                  "model":thread.get("model"), "reasoning_effort":thread.get("reasoningEffort"),
                                  "metadata":_metadata(thread), "metadata_version":2,
                                  "metadata_sha256":_digest(thread), "phase":"pending"})
            cursor = reply.get("nextCursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("Native thread pagination repeated a cursor")
            seen.add(cursor)
    if len({row["id"] for row in found}) != len(found):
        raise ValueError("Native thread inventory contains duplicate chat IDs")
    return found


def _metadata_for_row(rpc, row):
    metadata = row.get("metadata")
    if row.get("metadata_version", 1) not in (1, 2):
        raise ValueError("Unknown chat metadata snapshot version")
    saved_digest = _digest if row.get("metadata_version", 1) == 2 else _raw_digest
    if metadata is None:
        # An interrupted older journal can be upgraded only from a matching
        # native record. A mismatching original requires verified recovery data.
        current = rpc.request("thread/read", {"threadId":row["id"], "includeTurns":False})["thread"]
        metadata = _metadata(current)
        if saved_digest(metadata) != row["metadata_sha256"]:
            raise ValueError("Original chat metadata snapshot is required for recovery")
        row["metadata"] = metadata
    if saved_digest(metadata) != row["metadata_sha256"]:
        raise ValueError("The saved chat metadata snapshot changed")
    return metadata


def _set_provider(rpc, row, target, archived_ids):
    metadata = _metadata_for_row(rpc, row)
    archived = row["archived"]
    if archived and row["id"] in archived_ids:
        rpc.request("thread/unarchive", {"threadId":row["id"]})
        archived_ids.discard(row["id"])
    try:
        # Changing provider otherwise selects its default model. Carry the
        # existing explicit model/effort through the native resume operation.
        params = {"threadId":row["id"], "modelProvider":target, "excludeTurns":True}
        # Resume otherwise reloads the initial rollout cwd, losing a workspace
        # move made later in the official desktop (especially archived chats).
        if metadata.get("cwd") is not None:
            params["cwd"] = metadata["cwd"]
        if row.get("model") is not None:
            params["model"] = row["model"]
        if row.get("reasoning_effort") is not None:
            params["config"] = {"model_reasoning_effort":row["reasoning_effort"]}
        result = rpc.request("thread/resume", params)
        thread = result["thread"]
        if thread["id"] != row["id"] or result.get("modelProvider", thread.get("modelProvider")) != target:
            raise ValueError("Native resume did not retain the chat ID and target provider")
        defaults = {key:result.get(key, thread.get(key)) for key in ('model', 'reasoningEffort')}
        if not metadata_matches(metadata, thread, resolved_defaults=defaults):
            raise ValueError("Unrelated native chat metadata changed during provider migration")
        rpc.request("thread/unsubscribe", {"threadId":row["id"]})
    finally:
        if archived:
            rpc.request("thread/archive", {"threadId":row["id"]})
            archived_ids.add(row["id"])


def migrate(rpc, providers, journal_path, *, target=PROVIDER):
    """Migrate the complete recorded inventory, resuming safely after interruption."""
    journal_path = Path(journal_path)
    if target not in (PROVIDER, 'openai'):
        raise ValueError('Unknown managed migration target')
    providers = set(providers) - {target}
    if journal_path.exists():
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if journal.get("format") != 1 or journal.get("target") != target or set(journal["providers"]) != providers:
            raise ValueError("A different provider migration owns this journal")
    else:
        journal = {"format":1, "target":target, "providers":sorted(providers), "phase":"prepared",
                   "inference_turns":0, "threads":inventory(rpc, providers)}
        atomic_json(journal_path, journal)
    if journal["phase"] == "rolled_back":
        raise ValueError("Choose a new migration journal after a rollback")
    if journal["phase"] == "complete":
        return {"status":"passed", "migrated_chats":len(journal["threads"]),
                "archived_chats":sum(row["archived"] for row in journal["threads"]), "inference_turns":0}
    archived_ids = {row['id'] for row in inventory(rpc, providers | {target}) if row['archived']}
    for row in journal["threads"]:
        if row["phase"] == "migrated":
            continue
        _metadata_for_row(rpc, row)
        row["phase"] = "changing"
        journal["phase"] = "running"
        atomic_json(journal_path, journal)
        try:
            _set_provider(rpc, row, target, archived_ids)
        except Exception:
            journal["phase"] = "needs_recovery"
            atomic_json(journal_path, journal)
            raise
        row["phase"] = "migrated"
        atomic_json(journal_path, journal)
    journal["phase"] = "complete"
    atomic_json(journal_path, journal)
    return {"status":"passed", "migrated_chats":len(journal["threads"]),
            "archived_chats":sum(row["archived"] for row in journal["threads"]), "inference_turns":0}


def rollback(rpc, journal_path):
    journal_path = Path(journal_path)
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("format") != 1 or journal.get("target") not in (PROVIDER, 'openai'):
        raise ValueError("Unknown native provider migration journal")
    archived_ids = {row['id'] for row in inventory(rpc, set(journal['providers']) | {journal['target']}) if row['archived']}
    for row in reversed(journal["threads"]):
        if row["phase"] not in ("changing", "migrated"):
            continue
        _metadata_for_row(rpc, row)
        atomic_json(journal_path, journal)
        _set_provider(rpc, row, row["provider"], archived_ids)
        row["phase"] = "restored"
        atomic_json(journal_path, journal)
    journal["phase"] = "rolled_back"
    atomic_json(journal_path, journal)
    return {"status":"restored", "inference_turns":0}
