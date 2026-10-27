"""Read-only official-client identity and the native monitor backend.

The monitor follows the vendor installation and owns only its adapter/config
build. Import staging/installation lazily so this module is also usable by the
signature checks at the beginning of those transactions.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import patch as p


ARCHITECTURE = "official-client-v1"


def read_manifest(directory):
    if not directory:
        return {}
    path = Path(directory) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def verify_official(app):
    """Validate vendor signatures without old patch/integrity byte anchors."""
    app = Path(app).resolve()
    if sys.platform == "linux":
        layout = p.app_layout(app)
        return {"identity": "npm-package-metadata", "package": "@openai/codex",
                "version": layout['version'], "cli_sha256": p.file_sha(layout['cli'])}
    if sys.platform == "win32":
        from windows_support import verify_publisher
        return {"publisher": verify_publisher(app)}
    p.require(sys.platform == "darwin", "Official Codex supports macOS and Windows")
    from official_client_host import mac_host_identity
    value = mac_host_identity(app)
    p.require(value["bundle_id"] == "com.openai.codex", "Unexpected official client identity")
    p.require(value["code_identifiers"].get(value["cli"]) == "codex",
              "Unexpected bundled official CLI identity")
    return {"team": value["team_id"], "bundle_id": value["bundle_id"],
            "code_identifiers": value["code_identifiers"]}


def managed_configuration(directory, manifest, home):
    """Validate only owned fields, preserving unrelated user configuration."""
    from official_client_config import PROVIDER
    owned = manifest.get("native_config")
    p.require(isinstance(owned, dict) and owned.get("format") == 1,
              "Native configuration ownership record is missing")
    current = p.tomllib.loads(p.read_config(Path(home) / "config.toml"))
    for key, expected in owned["root"].items():
        p.require(current.get(key) == expected, "A managed native setting changed: " + key)
    # Compression belongs to the native client/user. Legacy false overrides
    # are migrated at staging; independent user changes must not block updates.
    if owned.get("provider") is not None:
        p.require(current.get("model_providers", {}).get(PROVIDER) == owned["provider"],
                  "The managed native provider changed")
    catalog = Path(directory) / "desktop-models.json"
    p.require(Path(current.get("model_catalog_json", "")).resolve() == catalog.resolve(),
              "Native catalog is not owned by this build")
    # Users can lower model windows directly; model_sync imports those limits
    # on its next pass. Validate the owned catalog without freezing its hash.
    p.require(catalog.is_file() and not catalog.is_symlink(), "The managed native catalog is missing")
    models = json.loads(catalog.read_text(encoding="utf-8")).get("models")
    p.require(isinstance(models, list) and models, "The native catalog has no models")
    identifiers = set()
    for model in models:
        slug = model.get("slug")
        p.require(isinstance(slug, str) and slug and slug not in identifiers,
                  "The native catalog contains an invalid model identity")
        identifiers.add(slug)
        window, maximum = model.get("context_window"), model.get("max_context_window")
        p.require(type(window) is int and type(maximum) is int and 0 < window <= maximum,
                  "The native catalog contains invalid context limits")
        compact = model.get("auto_compact_token_limit")
        p.require(compact is None or type(compact) is int and 0 < compact <= maximum,
                  "The native catalog contains an invalid compaction limit")
    p.require(set(manifest.get("model_routes", {})) <= identifiers,
              "A managed route disappeared from the native catalog")
    p.catalog_template({"models": models})
    checks = {"adapter-profile.json": manifest["profile_sha256"],
              **manifest.get("runtime_sha256", {})}
    if manifest.get("host_settings_sha256"):
        checks["host-settings.json"] = manifest["host_settings_sha256"]
    for name, digest in checks.items():
        path = Path(directory) / name
        p.require(path.is_file() and not path.is_symlink() and p.file_sha(path) == digest,
                  "The managed native build changed: " + name)
    return current


def select_backend(settings, baseline=None):
    """Keep legacy recovery until a native installation has actually committed."""
    baseline = baseline or settings.get("recovery_build") or settings.get("baseline")
    record = read_manifest(baseline)
    selected = dict(settings)
    if baseline:
        selected["baseline"] = str(baseline)
    if record.get("architecture") == ARCHITECTURE:
        return NativeMonitorBackend(selected)
    from backend import MonitorBackend
    return MonitorBackend(selected)


class NativeMonitorBackend:
    def __init__(self, settings):
        self.settings = settings
        self.baseline = Path(settings["baseline"]) if settings.get("baseline") else None
        record = read_manifest(self.baseline)
        self.app = Path(record.get("target") or settings["app"])
        self.store = False
        if sys.platform == "win32":
            from windows_support import is_store_app
            self.store = is_store_app(self.app)

    def refresh_source(self):
        """Locate a Store update before discovery; signature/hash checks follow."""
        if self.store:
            from windows_support import discover_app
            self.app = discover_app()

    def snapshot(self):
        self.refresh_source()
        verify_official(self.app)
        from backend import snapshot_app
        return snapshot_app(self.app)

    def inspect(self, snapshot):
        try:
            import official_client
            return official_client.inspect(self.app)
        except Exception as error:
            return {"compatible": False, "reason": str(error)}

    def build(self, snapshot, output):
        p.require(self.snapshot()["fingerprint"] == snapshot["fingerprint"], "App changed before native build")
        import official_client
        manifest = official_client.stage(app=self.app, codex_home=Path(self.settings["codex_home"]),
            output=Path(output), baseline=self.baseline, edition=self.settings.get("edition"))
        from update_worker import verify_build
        verify_build(output)
        p.require(manifest["architecture"] == ARCHITECTURE, "Unexpected native build architecture")
        return {"directory": str(output)}

    def is_running(self):
        try:
            # An obsolete patched deployment is not the native client. Its
            # first-migration stop check remains in the legacy backend.
            p.require_stopped(self.app, read_only_source=sys.platform == "win32")
            return False
        except (p.PatchError, ValueError):
            return True

    def install(self, build, snapshot):
        p.require(self.snapshot()["fingerprint"] == snapshot["fingerprint"], "App changed before native installation")
        directory = Path(build["directory"])
        from official_client_install import install
        install(directory, read_manifest(directory))
        self.baseline = directory
        return {"installed_fingerprint": self.snapshot()["fingerprint"], "baseline": str(directory)}

    def adopt(self):
        recovery = self.settings.get("recovery_build")
        if recovery and read_manifest(recovery).get("status") == "installed":
            self.baseline = Path(recovery)
        record = read_manifest(self.baseline)
        if record.get("architecture") != ARCHITECTURE or record.get("status") != "installed":
            return None
        if not self.app.exists():
            return None
        current = self.snapshot()
        if p.file_sha(p.app_layout(self.app)["asar"]) != record.get("original_asar"):
            return None  # The vendor updated; prepare the next native build.
        p.require(record.get("edition") == self.settings.get("edition", p.EDITION),
                  "Native baseline belongs to another edition")
        managed_configuration(self.baseline, record, self.settings["codex_home"])
        return {"fingerprint": current["fingerprint"], "baseline": str(self.baseline),
                "build": {"directory": str(self.baseline)}}
