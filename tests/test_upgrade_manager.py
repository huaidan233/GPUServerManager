"""Host transaction tests with real files/SQLite and an isolated Docker model."""
import hashlib
from contextlib import closing, contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid
import zipfile

import pytest


MODULE = Path(__file__).resolve().parents[1] / "scripts/deploy/upgrade_manager.py"
SPEC = importlib.util.spec_from_file_location("host_upgrade_manager", MODULE)
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


@contextmanager
def db(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class DockerModel:
    def __init__(self):
        self.calls = []
        self.images = {name: f"sha256:old-{name}" for name in ["api", "web", "server-agent", "wechat-agent"]}
        self.running = {name: True for name in self.images}
        self.research = {"research-job": {"running": True, "image": "research-original"}}
        self.fail_build = False
        self.fail_migration = False
        self.fail_new_health = False
        self.fail_all_health = False
        self.fail_stop = False
        self.migrate = lambda: None
        self.storage_override = None
        self.oneoff_running = False

    def run(self, args, *, env=None, timeout=300):
        self.calls.append((list(args), env))
        if args[:3] == ["docker", "container", "ls"]:
            query = next((value for value in args if value.startswith("id=")), None)
            if query:
                service = query.removeprefix("id=container-")
                if service == "migration":
                    return "running" if self.oneoff_running else ""
                return "running" if self.running[service] else "exited"
            assert "label=com.docker.compose.project=gsm-test" in args
            service = next(value.split("=", 2)[2] for value in args if value.startswith("label=com.docker.compose.service="))
            return "container-" + service + ("\ncontainer-migration" if service == "api" and self.oneoff_running else "")
        if args[:3] == ["docker", "container", "stop"]:
            assert args[3:5] == ["--timeout", "-1"]
            service = args[-1].removeprefix("container-")
            if service == "migration":
                self.oneoff_running = False
                return ""
            assert service in upgrade.CONTROL
            if not self.fail_stop:
                self.running[service] = False
            return ""
        if args[:3] == ["docker", "image", "tag"]:
            return ""
        if args[:3] == ["docker", "image", "inspect"]:
            return "sha256:new-" + args[-1].split("-")[-1].split(":")[0]
        if args[:2] == ["docker", "inspect"]:
            service = args[-1].removeprefix("container-")
            if "--format" in args:
                if "{{.State.Running}}" in args:
                    return "true" if self.running[service] else "false"
                return self.images[service]
            healthy = not self.fail_all_health and not (self.fail_new_health and self.images[service].startswith("sha256:new"))
            return json.dumps([{"Image": self.images[service], "State": {
                "Running": self.running[service], "Health": {"Status": "healthy" if healthy else "unhealthy"}}}])
        assert args[:2] == ["docker", "compose"]
        index = 2
        files = []
        while args[index].startswith("--") or args[index] == "-f":
            if args[index] == "-f":
                files.append(Path(args[index + 1]))
            index += 2
        command = args[index:]
        assert "down" not in command
        if command[0] == "ps":
            if "--format" in command:
                return json.dumps([{"Service": name, "ID": "container-" + name, "State": "running" if live else "exited"}
                                   for name, live in self.running.items()])
            return "container-" + command[-1]
        if command[0] == "config":
            if "--format" not in command:
                return ""
            if self.storage_override:
                return json.dumps(self.storage_override)
            return json.dumps({"services": {
                "api": {"environment": {key: "sqlite:////var/lib/gpu-server-manager/" + name for key, name in [
                    ("GSM_DATABASE_URL", "control.sqlite"), ("GSM_TELEMETRY_DATABASE_URL", "telemetry.sqlite"),
                    ("GSM_ANALYTICS_DATABASE_URL", "analytics.sqlite")]},
                        "volumes": [{"type": "bind", "source": str(self.state), "target": "/var/lib/gpu-server-manager"}]},
                "server-agent": {"environment": {}, "volumes": [{"type": "bind", "source": str(self.state / "server-agent"),
                    "target": "/var/lib/gpu-server-manager/server-agent"}]}}})
        if command[0] == "build":
            if self.fail_build:
                raise upgrade.UpgradeError("build_failed")
            return ""
        if command[0] == "stop":
            assert command[1:3] == ["--timeout", "-1"]
            for service in command[3:]:
                assert service in upgrade.CONTROL
                if not self.fail_stop:
                    self.running[service] = False
            return ""
        if command[0] == "run":
            assert command[1:5] == ["--rm", "--no-deps", "-T", "--name"]
            assert command[5].startswith("gsm-upgrade-") and command[5].endswith("-migration")
            assert command[6:10] == ["api", "python", "-m", "gsm_api.upgrade_migrations"]
            assert command[-2:] == ["--target-version", "0.3.0-rc.2"]
            self.migrate()
            if self.fail_migration:
                raise upgrade.UpgradeError("migration_failed")
            return ""
        if command[0] == "up":
            if command == ["up", "-d"]:
                return ""
            assert "--no-deps" in command and "--no-build" in command
            override = {}
            for path in files:
                if path.suffix == ".json":
                    for name, settings in json.loads(path.read_text())["services"].items():
                        override.setdefault(name, {}).update(settings)
            for service in command[6:]:
                assert service in upgrade.CONTROL
                image = override.get(service, {}).get("image", self.images[service])
                if image.startswith("gsm-release"):
                    image = "sha256:new-" + image.split("-")[-1].split(":")[0]
                self.images[service] = image
                self.running[service] = True
            return ""
        if command[0] == "exec":
            return ""
        raise AssertionError(command)


def package(tmp_path, *, extra=None):
    archive = tmp_path / (uuid.uuid4().hex + ".zip")
    content = {"package.json": json.dumps({"version": "0.3.0-rc.2"}),
               "scripts/deploy/docker-compose.yml": "services: {}\n",
               "services/api/gsm_api/upgrade_migrations.py": "# migration entrypoint\n"}
    content.update(extra or {})
    with zipfile.ZipFile(archive, "w") as target:
        for name, data in content.items():
            info = zipfile.ZipInfo("gpu-server-manager-0.3.0-rc.2/" + name)
            info.filename = "gpu-server-manager-0.3.0-rc.2/" + name
            target.writestr(info, data)
    manifest = {"schema_version": 1, "product": "gpu-server-manager", "version": "0.3.0-rc.2",
                "update_protocol": 1, "schema_revision": 1,
                "archive": "gpu-server-manager-0.3.0-rc.2-source.zip", "install_mode": "docker-compose-source",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "size": archive.stat().st_size}
    path = tmp_path / (uuid.uuid4().hex + ".json")
    path.write_text(json.dumps(manifest))
    return archive, path


def fixture(tmp_path):
    app, config, state = [tmp_path / name for name in ["app", "configuration", "state"]]
    for folder in [app, config, state]:
        folder.mkdir()
    source = app / "source"
    (source / "scripts/deploy").mkdir(parents=True)
    (source / "scripts/deploy/docker-compose.yml").write_text("services: {}\n")
    compose = app / "docker-compose.yml"
    compose.write_text("services: {}\n")
    (config / "gsm.env").write_text(f"GSM_APP_SOURCE={source.as_posix()}\nCOMPOSE_PROJECT_NAME=gsm-test\nGSM_DEPLOYMENT_ID=test\n")
    (config / "api.env").write_text("SECRET=unchanged-test-value\n")
    (state / "user-upload.pdf").write_bytes(b"original personal upload")
    database = state / "control.sqlite"
    with db(database) as connection:
        connection.execute("CREATE TABLE members (name TEXT)")
        connection.execute("INSERT INTO members VALUES ('existing-member')")
    model = DockerModel()
    model.state = state
    manager = upgrade.UpgradeManager({"app_root": str(app), "config_dir": str(config), "state_dir": str(state),
                                      "compose_files": [str(compose)], "project_name": "gsm-test", "health_timeout": 0,
                                      "minimum_free_bytes": 0, "current_version": "0.3.0-rc.1"}, commands=model, enforce_permissions=False)
    manager._drain = lambda: None
    manager.executor_status = lambda: manager.status()
    return manager, model, database


def run_request(manager, **kwargs):
    result = manager.request({"request_id": uuid.uuid4().hex, **kwargs})
    manager.worker.join(10)
    assert not manager.worker.is_alive()
    return result, manager.status()["operation"]


def read_members(database):
    with db(database) as connection:
        return connection.execute("SELECT name FROM members ORDER BY name").fetchall()


def test_successful_upgrade_keeps_users_data_project_and_secrets(tmp_path):
    manager, model, database = fixture(tmp_path)
    archive, manifest = package(tmp_path)
    prepared = manager.stage_local(archive, manifest)
    original_config = (manager.configuration / "api.env").read_bytes()
    response, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert response["schema_version"] == 1
    assert operation["state"] == "succeeded"
    assert operation["rollback_available"]
    assert read_members(database) == [("existing-member",)]
    assert (manager.configuration / "api.env").read_bytes() == original_config
    assert "GSM_DEPLOYMENT_ID=test" in (manager.configuration / "gsm.env").read_text()
    assert (manager.state / "user-upload.pdf").read_bytes() == b"original personal upload"
    assert model.research["research-job"] == {"running": True, "image": "research-original"}
    assert not (manager.public / "maintenance.json").exists()
    assert manager.status()["current_version"] == "0.3.0-rc.2"
    for command, _ in model.calls:
        if command[:2] == ["docker", "compose"]:
            assert command[2:4] == ["--project-name", "gsm-test"]
            assert command[command.index("--project-directory") + 1] == str(manager.compose_files[0].parent)


@pytest.mark.parametrize("fault", ["fail_migration", "fail_new_health"])
def test_failed_migration_or_health_restores_database_config_and_old_images(tmp_path, fault):
    manager, model, database = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    old_config = (manager.configuration / "gsm.env").read_bytes()
    def migrate():
        with db(database) as connection:
            connection.execute("INSERT INTO members VALUES ('new-schema-write')")
        (manager.configuration / "api.env").write_text("SECRET=modified-by-migration\n")
        with db(manager.state / "new.sqlite") as connection:
            connection.execute("CREATE TABLE new_table (value TEXT)")
    model.migrate = migrate
    setattr(model, fault, True)
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["state"] == "rolled_back"
    assert read_members(database) == [("existing-member",)]
    assert (manager.configuration / "gsm.env").read_bytes() == old_config
    assert (manager.configuration / "api.env").read_text() == "SECRET=unchanged-test-value\n"
    assert not (manager.state / "new.sqlite").exists()
    assert all(image.startswith("sha256:old") for image in model.images.values())
    assert not (manager.public / "maintenance.json").exists()
    assert model.research["research-job"]["running"]


def test_migration_oneoff_is_stopped_and_removal_confirmed_before_rollback(tmp_path):
    manager, model, database = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    model.fail_migration = True
    def long_running_migration():
        model.oneoff_running = True
        with db(database) as connection:
            connection.execute("DELETE FROM members")
    model.migrate = long_running_migration
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["state"] == "rolled_back"
    assert model.oneoff_running is False
    assert any(command[-1] == "container-migration" and command[:3] == ["docker", "container", "stop"] for command, _ in model.calls)
    assert read_members(database) == [("existing-member",)]


def test_build_failure_never_enters_maintenance_or_stops_services(tmp_path):
    manager, model, database = fixture(tmp_path)
    model.fail_build = True
    prepared = manager.stage_local(*package(tmp_path))
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["state"] == "failed"
    assert all(model.running.values())
    assert not (manager.public / "maintenance.json").exists()
    assert read_members(database) == [("existing-member",)]


def test_stop_failure_never_backs_up_a_live_writer(tmp_path):
    manager, model, _ = fixture(tmp_path)
    model.fail_stop = True
    prepared = manager.stage_local(*package(tmp_path))
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    transaction = manager.private / "transactions" / operation["id"]
    assert not (transaction / "backup/index.json").exists()
    assert operation["state"] == "rolled_back"


def test_wal_commits_and_all_database_families_are_in_snapshot(tmp_path):
    manager, _, database = fixture(tmp_path)
    with db(database) as active:
        active.execute("PRAGMA journal_mode=WAL")
        active.execute("PRAGMA wal_autocheckpoint=0")
        active.execute("INSERT INTO members VALUES ('wal-committed')")
        active.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        for relative in ["telemetry.sqlite", "analytics.sqlite", "server-agent/receipts.sqlite3"]:
            path = manager.state / relative
            path.parent.mkdir(exist_ok=True)
            with db(path) as connection:
                connection.execute("CREATE TABLE data (value TEXT)")
        transaction = manager.private / "transactions" / uuid.uuid4().hex
        transaction.mkdir()
        index = manager._snapshot(transaction)
        assert len(index["databases"]) == 4
    with db(database) as connection:
        connection.execute("DELETE FROM members")
    manager._restore_snapshot(transaction)
    assert read_members(database) == [("existing-member",), ("wal-committed",)]
    assert not Path(str(database) + "-wal").exists()


def test_corrupt_backup_is_rejected_before_replacing_current_data(tmp_path):
    manager, _, database = fixture(tmp_path)
    transaction = manager.private / "transactions" / uuid.uuid4().hex
    transaction.mkdir()
    index = manager._snapshot(transaction)
    (transaction / "backup" / index["databases"][0]["backup"]).write_bytes(b"corrupted")
    with pytest.raises(upgrade.UpgradeError, match="backup_corrupt"):
        manager._restore_snapshot(transaction)
    assert read_members(database) == [("existing-member",)]


def test_corrupt_configuration_backup_is_refused_before_overwriting_secrets(tmp_path):
    manager, _, _ = fixture(tmp_path)
    transaction = manager.private / "transactions" / uuid.uuid4().hex
    transaction.mkdir()
    manager._snapshot(transaction)
    (transaction / "backup/configuration/api.env").write_text("damaged backup")
    current = manager.configuration / "api.env"
    current.write_text("newer-configuration")
    with pytest.raises(upgrade.UpgradeError, match="backup_corrupt"):
        manager._restore_snapshot(transaction)
    assert current.read_text() == "newer-configuration"


def test_failed_command_diagnostics_are_bounded_private_and_not_returned(tmp_path, monkeypatch):
    import subprocess
    private = tmp_path / "private"
    commands = upgrade.Commands(private)
    diagnostic = "upstream-private-detail" + "x" * 50000
    monkeypatch.setattr(upgrade.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, stdout="", stderr=diagnostic))
    with pytest.raises(upgrade.UpgradeError, match="^host_command_failed$"):
        commands.run(["docker", "compose", "build"])
    files = list(private.glob("*.json"))
    assert len(files) == 1
    logged = json.loads(files[0].read_text())
    assert len(logged["stderr"]) == 32768
    if os.name == "posix":
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600


def test_request_id_is_durable_idempotent_and_bound_to_exact_payload(tmp_path):
    manager, _, _ = fixture(tmp_path)
    manager.stage_local(*package(tmp_path))
    request = {"action": "prepare", "version": "0.3.0-rc.2", "request_id": uuid.uuid4().hex}
    first = manager.request(request)
    manager.worker.join(10)
    repeated = manager.request(request)
    assert repeated["replayed"] is True
    assert repeated["operation"]["id"] == first["operation"]["id"]
    with pytest.raises(upgrade.UpgradeError, match="different_content"):
        manager.request({**request, "version": "0.4.0"})


@pytest.mark.parametrize("extra", [{"path": "/tmp/package.zip"}, {"command": "sh -c anything"}, {"repository": "untrusted/repo"}])
def test_web_requests_cannot_supply_executable_paths_or_repositories(tmp_path, extra):
    manager, _, _ = fixture(tmp_path)
    with pytest.raises(upgrade.UpgradeError, match="schema"):
        manager.request({"action": "prepare", "version": "0.3.0-rc.2", "request_id": uuid.uuid4().hex, **extra})


def test_expired_preparation_and_custom_base_are_refused(tmp_path):
    manager, model, _ = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    path = manager.private / "prepared" / (prepared["id"] + ".json")
    upgrade.write_json(path, {**prepared, "expires_at": "2000-01-01T00:00:00+00:00"})
    with pytest.raises(upgrade.UpgradeError, match="expired"):
        manager.request({"action": "apply", "prepared_id": prepared["id"], "request_id": uuid.uuid4().hex})
    prepared = manager.stage_local(*package(tmp_path))
    manager.compose_files[0].write_text("custom deployment settings\n")
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["message"] == "custom_compose_requires_overlay"
    assert not model.calls


@pytest.mark.parametrize("change", ["database_environment", "volume_shadow", "named_volume", "receipt_environment", "settings_environment"])
def test_effective_compose_database_locations_must_match_backed_up_state(tmp_path, change):
    manager, model, _ = fixture(tmp_path)
    rendered = json.loads(manager.compose("config", "--format", "json"))
    if change == "database_environment":
        rendered["services"]["api"]["environment"]["GSM_DATABASE_URL"] = "sqlite:////outside/control.sqlite"
    elif change == "volume_shadow":
        rendered["services"]["api"]["volumes"].append({"type": "bind", "source": str(tmp_path / "outside.sqlite"),
                                                        "target": "/var/lib/gpu-server-manager/control.sqlite"})
    elif change == "named_volume":
        rendered["services"]["api"]["volumes"][0]["type"] = "volume"
    elif change == "receipt_environment":
        rendered["services"]["server-agent"]["environment"]["GSM_AGENT_RECEIPT_DB_PATH"] = "/outside/receipts.sqlite"
    else:
        rendered["services"]["server-agent"]["environment"]["GSM_AGENT_SETTINGS_STATE_DIR"] = "/outside/settings"
    model.storage_override = rendered
    prepared = manager.stage_local(*package(tmp_path))
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["state"] == "failed"
    assert all(model.running.values())
    assert not (manager.public / "maintenance.json").exists()


def test_public_ipc_is_outside_api_owned_state_namespace(tmp_path):
    manager, _, _ = fixture(tmp_path)
    assert manager.public == manager.app / "updates-public"
    assert not manager.public.is_relative_to(manager.state)


def test_management_boot_gate_blocks_incomplete_recovery_and_maintenance(tmp_path):
    manager, model, _ = fixture(tmp_path)
    upgrade.write_json(manager.private / "operation.json", {"state": "recovery_required"})
    with pytest.raises(upgrade.UpgradeError, match="recovery_completes"):
        manager.start_control_plane()
    assert not model.calls
    upgrade.write_json(manager.private / "operation.json", {"state": "succeeded"})
    upgrade.write_json(manager.public / "maintenance.json", {"operation_id": uuid.uuid4().hex})
    with pytest.raises(upgrade.UpgradeError, match="recovery_completes"):
        manager.start_control_plane()
    manager._clear_maintenance()
    manager.start_control_plane()
    assert model.calls[-1][0][-2:] == ["up", "-d"]


def test_management_boot_gate_requires_a_ready_executor(tmp_path):
    manager, model, _ = fixture(tmp_path)
    manager.executor_status = lambda: {"schema_version": 1, "executor": {"online": False}}
    with pytest.raises(upgrade.UpgradeError, match="online_executor"):
        manager.start_control_plane()
    assert not model.calls


@pytest.mark.parametrize("name", ["../outside", "/absolute", "nested/../../outside", "nested\\evil", "C:/evil"])
def test_release_extraction_refuses_unsafe_paths(tmp_path, name):
    archive, path = package(tmp_path, extra={name: "bad"})
    with pytest.raises(upgrade.UpgradeError, match="unsafe_release_archive"):
        upgrade.extract_verified(archive, json.loads(path.read_text()), tmp_path / "source")
    assert not (tmp_path / "source").exists()


def test_release_extraction_refuses_symlinks(tmp_path):
    archive, path = package(tmp_path)
    with zipfile.ZipFile(archive, "a") as source:
        info = zipfile.ZipInfo("gpu-server-manager-0.3.0-rc.2/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        source.writestr(info, "/etc/shadow")
    manifest = json.loads(path.read_text())
    manifest.update(size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    with pytest.raises(upgrade.UpgradeError, match="unsafe_release_archive"):
        upgrade.extract_verified(archive, manifest, tmp_path / "source")


def test_recovery_after_crash_restores_completed_snapshot(tmp_path):
    manager, model, database = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    # Simulate process death after the backup and migration, before the new services start.
    def crash_start(*args, **kwargs):
        raise KeyboardInterrupt()
    original_start = manager._start
    manager._start = crash_start
    operation = {"id": uuid.uuid4().hex, "action": "apply", "created_at": upgrade.now(), "target_version": "0.3.0-rc.2"}
    (manager.private / "transactions" / operation["id"]).mkdir()
    def migrate():
        with db(database) as connection:
            connection.execute("DELETE FROM members")
    model.migrate = migrate
    with pytest.raises(KeyboardInterrupt):
        manager.apply(prepared["id"], operation)
    assert (manager.public / "maintenance.json").exists()
    manager._start = original_start
    manager.recover()
    assert manager.status()["operation"]["state"] == "rolled_back"
    assert read_members(database) == [("existing-member",)]
    assert not (manager.public / "maintenance.json").exists()


def test_recovery_boots_after_configuration_restore_was_interrupted(tmp_path):
    manager, model, database = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    _, installed = run_request(manager, action="apply", prepared_id=prepared["id"])
    interrupted = {**installed, "state": "running", "phase": "rolling_back"}
    upgrade.write_json(manager.private / "operation.json", interrupted)
    # Simulate an interruption after old config files were removed, before copytree.
    for path in manager.configuration.iterdir():
        path.unlink()
    model.running = {service: True for service in model.running}
    recovered = upgrade.UpgradeManager(manager.config, commands=model, enforce_permissions=False)
    recovered.recover()
    assert recovered.status()["operation"]["state"] == "rolled_back"
    assert (recovered.configuration / "gsm.env").exists()
    assert (recovered.configuration / "api.env").read_text() == "SECRET=unchanged-test-value\n"
    assert read_members(database) == [("existing-member",)]
    assert model.research["research-job"]["running"]


def test_failed_rollback_keeps_maintenance_and_requires_recovery(tmp_path):
    manager, model, _ = fixture(tmp_path)
    model.fail_all_health = True
    prepared = manager.stage_local(*package(tmp_path))
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    assert operation["state"] == "recovery_required"
    assert (manager.public / "maintenance.json").exists()
    with pytest.raises(upgrade.UpgradeError, match="in_progress"):
        manager.request({"action": "prepare", "version": "0.3.0-rc.2", "request_id": uuid.uuid4().hex})
    model.fail_all_health = False
    manager.recover()
    assert manager.status()["operation"]["state"] == "rolled_back"


def test_manual_rollback_requires_confirmation_and_latest_transaction(tmp_path):
    manager, _, database = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    _, operation = run_request(manager, action="apply", prepared_id=prepared["id"])
    request = {"action": "rollback", "transaction_id": operation["id"], "request_id": uuid.uuid4().hex,
               "acknowledge_data_loss": False}
    with pytest.raises(upgrade.UpgradeError, match="data_loss_confirmation"):
        manager.request(request)
    with db(database) as connection:
        connection.execute("INSERT INTO members VALUES ('written-after-upgrade')")
    manager.request({**request, "acknowledge_data_loss": True})
    manager.worker.join(10)
    assert manager.status()["operation"]["state"] == "rolled_back"
    assert read_members(database) == [("existing-member",)]


def test_preparing_another_package_does_not_hide_current_release_rollback(tmp_path):
    manager, _, _ = fixture(tmp_path)
    prepared = manager.stage_local(*package(tmp_path))
    _, installed = run_request(manager, action="apply", prepared_id=prepared["id"])
    _, prepared_again = run_request(manager, action="prepare", version="0.3.0-rc.2")
    assert prepared_again["state"] == "succeeded"
    assert prepared_again["rollback_available"] is True
    assert prepared_again["rollback_id"] == installed["id"]
    _, failed_prepare = run_request(manager, action="prepare", version="0.4.0")
    assert failed_prepare["state"] == "failed"
    assert failed_prepare["rollback_id"] == installed["id"]


@pytest.mark.parametrize("changes", [{"update_protocol": 2}, {"schema_revision": 0}, {"schema_revision": 2**31}, {"update_protocol": True}, {"schema_revision": None}])
def test_future_protocol_or_invalid_schema_is_refused(tmp_path, changes):
    _, path = package(tmp_path)
    with pytest.raises(upgrade.UpgradeError, match="invalid_release_manifest"):
        upgrade.validate_manifest({**json.loads(path.read_text()), **changes})


def test_new_positive_schema_revision_is_delegated_to_release_migration_code(tmp_path):
    _, path = package(tmp_path)
    manifest = {**json.loads(path.read_text()), "schema_revision": 2}
    assert upgrade.validate_manifest(manifest)["schema_revision"] == 2


def test_agent_runtime_configuration_is_restored_with_its_permissions(tmp_path):
    manager, _, _ = fixture(tmp_path)
    folder = manager.state / "server-agent/settings"
    folder.mkdir(parents=True)
    original = folder / "container.json"
    original.write_text('{"cpu":8}')
    transaction = manager.private / "transactions" / uuid.uuid4().hex
    transaction.mkdir()
    manager._snapshot(transaction)
    original.write_text('{"cpu":64}')
    (folder / "created-after-upgrade.json").write_text("{}")
    manager._restore_snapshot(transaction)
    assert original.read_text() == '{"cpu":8}'
    assert not (folder / "created-after-upgrade.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="Linux host advisory lock")
def test_linux_executor_lock_refuses_second_process(tmp_path):
    with upgrade.single_instance(tmp_path / "lock"):
        with pytest.raises(upgrade.UpgradeError, match="already_running"):
            with upgrade.single_instance(tmp_path / "lock"):
                pytest.fail("second executor acquired exclusive lock")


@pytest.mark.skipif(os.name != "posix", reason="Linux root directory trust checks")
def test_writable_root_ancestor_is_not_a_trusted_executor_location(tmp_path):
    parent = tmp_path / "writable"
    parent.mkdir(mode=0o777)
    os.chmod(parent, 0o777)
    app = parent / "application"
    app.mkdir()
    with pytest.raises(upgrade.UpgradeError, match="untrusted_root_directory"):
        upgrade.trusted_root_directory(app)


@pytest.mark.skipif(os.name != "posix", reason="Linux deployment operation lock")
def test_management_start_and_upgrade_share_a_process_lock(tmp_path):
    import multiprocessing
    manager, _, _ = fixture(tmp_path)
    context = multiprocessing.get_context("fork")
    entered = context.Event()
    def enter():
        with manager.deployment_guard():
            entered.set()
    with manager.deployment_guard():
        process = context.Process(target=enter)
        process.start()
        assert not entered.wait(0.1)
    try:
        assert entered.wait(5)
    finally:
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)


@pytest.mark.skipif(os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0, reason="Linux root Unix peer credentials")
def test_linux_socket_status_is_available_without_any_docker_operation(tmp_path):
    import multiprocessing
    import time
    manager, model, _ = fixture(tmp_path)
    process = multiprocessing.get_context("fork").Process(target=upgrade.serve, args=(manager,))
    process.start()
    try:
        path = manager.public / "manager.sock"
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        status = upgrade.socket_request(path, {"action": "status"})
        assert status["schema_version"] == 1
        assert status["executor"]["online"]
        refused = upgrade.socket_request(path, {"action": "status", "command": "untrusted"})
        assert refused["accepted"] is False
        assert not model.calls
    finally:
        process.terminate()
        process.join(5)
