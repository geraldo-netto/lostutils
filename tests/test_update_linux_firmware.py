import os
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPO_ROOT / "update-linux-firmware.sh"
PINNED_FPR = "4CDE8575E547BF835FE15807A31B6BD72486CFD6"


class UpdaterHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.fake_bin = root / "bin"
        self.firmware = root / "firmware"
        self.cache = root / "cache"
        self.state = root / "state"
        self.work = root / "work"
        self.backup = root / "firmware.backup"
        self.release = "linux-firmware-20260201"
        self.installed_release = "linux-firmware-20260101"
        self.stamp = self.state / "release.version"
        self.recovery = Path(f"{self.stamp}.recovery")
        self.pending = self.state / "initramfs.pending"
        self.old_stamp = self.state / "git.commit"
        self.call_log = root / "calls.log"
        self.initramfs_log = root / "initramfs.log"
        self.sync_log = root / "sync.log"
        self.updater_pid = root / "updater.pid"
        for directory in (
            self.fake_bin,
            self.firmware / "vendor",
            self.cache,
            self.state,
            self.work,
        ):
            directory.mkdir(parents=True)
        self.state.chmod(0o755)
        (self.firmware / "vendor" / "device.bin").write_text(
            "original firmware\n", encoding="utf-8"
        )
        self.stamp.write_text(f"{self.installed_release}\n", encoding="utf-8")
        self.archive = self.cache / f"{self.release}.tar.gz"
        self._create_archive()
        self._install_fake_commands()
        self._install_recovery_commands()

    def _write_command(self, name: str, source: str) -> None:
        path = self.fake_bin / name
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _create_archive(self) -> None:
        source = self.root / "archive" / self.release
        source.mkdir(parents=True)
        installer = source / "copy-firmware.sh"
        installer.write_text(
            textwrap.dedent(
                """
                #!/bin/sh
                destination=
                for argument do
                    destination=$argument
                done
                suffix=
                case " $* " in
                    *" --zstd "*) suffix=.zst ;;
                esac
                mkdir -p "$destination/vendor"
                printf 'updated firmware\n' > "$destination/vendor/device.bin$suffix"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        installer.chmod(0o755)
        with tarfile.open(self.archive, "w:gz") as bundle:
            bundle.add(source, arcname=self.release)

    def _install_fake_commands(self) -> None:
        self._write_command(
            "curl",
            r"""
            #!/usr/bin/env bash
            set -eu
            printf '%s\n' "$*" >> "$FAKE_CALL_LOG"
            [ "${FAIL_NETWORK:-0}" = 0 ] || exit 7
            output=
            url=
            while [ "$#" -gt 0 ]; do
                case "$1" in
                    -o)
                        output=$2
                        shift 2
                        ;;
                    *)
                        url=$1
                        shift
                        ;;
                esac
            done
            if [ -z "$output" ]; then
                printf '<a href="%s.tar.gz">fixture</a>\n' "$FAKE_RELEASE"
            elif [[ "$url" == *.tar.sign ]]; then
                printf 'fixture signature\n' > "$output"
            else
                cp -- "$FAKE_ARCHIVE" "$output"
            fi
            """,
        )
        self._write_command(
            "gpg",
            r"""
            #!/usr/bin/env bash
            set -eu
            if [[ " $* " == *" --verify "* ]]; then
                printf '[GNUPG:] VALIDSIG %s 0 0 0 0 0 0 0 %s\n' \
                    "$PINNED_FPR" "$PINNED_FPR"
            fi
            """,
        )
        self._write_command(
            "id",
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = -u ]; then
                printf '0\n'
            else
                exec /usr/bin/id "$@"
            fi
            """,
        )
        self._write_command(
            "stat",
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = -c ] && [ "${2:-}" = %u ]; then
                if [ "${*: -1}" = "${FAKE_UNTRUSTED_PATH:-}" ]; then
                    printf '1001\n'
                    exit 0
                fi
                printf '0\n'
                exit 0
            fi
            exec /usr/bin/stat "$@"
            """,
        )
        self._write_command(
            "rsync",
            r"""
            #!/usr/bin/env bash
            set -eu
            for argument in "$@"; do
                if [ "$argument" = -aHnc ]; then
                    if [ "${FAIL_BACKUP_VERIFY:-0}" = error ]; then
                        exit 23
                    fi
                    if [ "${FAIL_BACKUP_VERIFY:-0}" = different ]; then
                        printf '>fcs....... vendor/device.bin\n'
                    fi
                    exit 0
                fi
            done
            arguments=("$@")
            count=${#arguments[@]}
            source=${arguments[count-2]}
            destination=${arguments[count-1]}
            mkdir -p -- "$destination"
            cp -a -- "$source"/. "$destination"/
            if [ "${CRASH_AT:-}" = install ]; then
                kill -KILL -- "-$(cat "$FAKE_UPDATER_PID")"
                exit 0
            fi
            if [ "${FAIL_INSTALL:-0}" = 1 ]; then
                exit 23
            fi
            """,
        )
        self._write_command(
            "mktemp",
            r"""
            #!/usr/bin/env bash
            set -eu
            if [ "$#" -eq 2 ] && [ "$1" = -d ] \
                    && [ "$2" = /var/tmp/firmware-update.XXXXXX ]; then
                exec /usr/bin/mktemp -d "$FAKE_WORK_ROOT/firmware-update.XXXXXX"
            fi
            if [ "${1:-}" = "${STAMP_FILE}.tmp.XXXXXX" ] \
                    && [ -n "${FAIL_STAMP_ONCE_FILE:-}" ] \
                    && [ ! -e "$FAIL_STAMP_ONCE_FILE" ]; then
                touch "$FAIL_STAMP_ONCE_FILE"
                exit 1
            fi
            exec /usr/bin/mktemp "$@"
            """,
        )
        self._write_command(
            "update-initramfs",
            r"""
            #!/usr/bin/env bash
            set -eu
            printf '%s\n' "$*" >> "$FAKE_INITRAMFS_LOG"
            if [ "${FAIL_INITRAMFS:-0}" = 1 ]; then
                exit 1
            fi
            """,
        )
        self._write_command("zstd", "#!/usr/bin/env bash\nexit 0\n")

    def _install_recovery_commands(self) -> None:
        # Kill only the fixture's isolated process group, including pipelines.
        self._write_command(
            "sync",
            r"""
            #!/usr/bin/env bash
            set -eu
            path=${*: -1}
            journal="${STAMP_FILE}.recovery"
            phase=absent
            if [ -f "$journal" ]; then
                phase=$(tr '\0' '\n' < "$journal" | sed -n '2p')
            fi
            stamped=$(cat "$STAMP_FILE" 2>/dev/null || echo none)
            printf '%s|%s|%s\n' "$path" "$phase" "$stamped" >> "$FAKE_SYNC_LOG"
            if [ "$phase" = committed ] && [ -n "${FAIL_COMMIT_SYNC_ONCE_FILE:-}" ] \
                    && [ ! -e "$FAIL_COMMIT_SYNC_ONCE_FILE" ]; then
                touch "$FAIL_COMMIT_SYNC_ONCE_FILE"
                exit 1
            fi
            case "${CRASH_AT:-}" in
                before_commit)
                    [ "$phase" = installing ] && [ "$stamped" = "$FAKE_RELEASE" ] \
                        && [ "$path" = "$(dirname "$STAMP_FILE")" ] || exit 0
                    ;;
                committed)
                    [ "$phase" = committed ] || exit 0
                    ;;
                rollback)
                    [ "$phase" = installing ] && [ "$path" = "$FW_DIR" ] || exit 0
                    ;;
                *) exit 0 ;;
            esac
            kill -KILL -- "-$(cat "$FAKE_UPDATER_PID")"
            """,
        )

    def run(self, *arguments: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}:/usr/bin:/bin",
                "FIRMWARE_URL": "https://fixture.invalid/firmware/",
                "FW_DIR": str(self.firmware),
                "STAMP_FILE": str(self.stamp),
                "PENDING_INITRAMFS_FILE": str(self.pending),
                "OLD_GIT_STAMP": str(self.old_stamp),
                "CACHE_DIR": str(self.cache),
                "BACKUP_DIR": str(self.backup),
                "REQUIRED_WORK_MB": "0",
                "REQUIRED_LIB_MB": "0",
                "PINNED_FPR": PINNED_FPR,
                "FAKE_RELEASE": self.release,
                "FAKE_ARCHIVE": str(self.archive),
                "FAKE_CALL_LOG": str(self.call_log),
                "FAKE_INITRAMFS_LOG": str(self.initramfs_log),
                "FAKE_WORK_ROOT": str(self.work),
                "FAKE_SYNC_LOG": str(self.sync_log),
                "FAKE_UPDATER_PID": str(self.updater_pid),
                "LC_ALL": "C",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            ["bash", "-c", 'printf "%s\\n" "$$" > "$FAKE_UPDATER_PID"; exec bash "$@"',
             "firmware-fixture", str(UPDATER), *arguments],
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            start_new_session=True,
            timeout=20,
            check=False,
        )


def _assert_original_firmware(harness: UpdaterHarness) -> None:
    assert (harness.firmware / "vendor" / "device.bin").read_text(
        encoding="utf-8"
    ) == "original firmware\n"
    assert not (harness.firmware / "vendor" / "device.bin.zst").exists()


def test_successful_install_commits_firmware_and_rebuilds_initramfs(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run()

    assert result.returncode == 0, result.stderr
    assert "Update Complete" in result.stdout
    assert (harness.firmware / "vendor" / "device.bin.zst").read_text(
        encoding="utf-8"
    ) == "updated firmware\n"
    assert not (harness.firmware / "vendor" / "device.bin").exists()
    assert (harness.backup / "vendor" / "device.bin").read_text(
        encoding="utf-8"
    ) == "original firmware\n"
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.release
    assert not harness.pending.exists()
    assert harness.initramfs_log.read_text(encoding="utf-8").strip() == "-u -k all"
    curl_calls = harness.call_log.read_text(encoding="utf-8")
    assert "--connect-timeout 20" in curl_calls
    assert "--speed-limit 1024" in curl_calls


def test_backup_difference_aborts_before_firmware_mutation(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run(FAIL_BACKUP_VERIFY="different")

    assert result.returncode != 0
    assert "Backup differs" in result.stderr
    _assert_original_firmware(harness)
    assert harness.backup.is_dir()
    assert not harness.pending.exists()
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.installed_release


def test_install_failure_restores_verified_backup_and_state(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run(FAIL_INSTALL="1")

    assert result.returncode != 0
    assert "Automatic rollback completed" in result.stderr
    _assert_original_firmware(harness)
    assert not harness.backup.exists()
    assert not harness.pending.exists()
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.installed_release


def test_stamp_failure_restores_verified_backup_and_previous_stamp(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    failure_marker = tmp_path / "stamp-failed-once"

    result = harness.run(FAIL_STAMP_ONCE_FILE=str(failure_marker))

    assert result.returncode != 0
    assert "could not commit installed release stamp" in result.stderr
    assert "Automatic rollback completed" in result.stderr
    _assert_original_firmware(harness)
    assert not harness.backup.exists()
    assert not harness.pending.exists()
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.installed_release


def test_failed_initramfs_rebuild_is_retried_without_reinstall(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    failed = harness.run(FAIL_INITRAMFS="1")

    assert failed.returncode != 0
    assert "do NOT restore the backup" in failed.stderr
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.release
    assert harness.pending.read_text(encoding="utf-8").strip() == harness.release
    assert harness.backup.is_dir()

    retried = harness.run()

    assert retried.returncode == 0, retried.stderr
    assert "Pending initramfs rebuild completed" in retried.stdout
    assert "nothing to do" in retried.stdout
    assert not harness.pending.exists()
    assert harness.initramfs_log.read_text(encoding="utf-8").splitlines() == [
        "-u -k all",
        "-u -k all",
    ]


def test_downgrade_requires_explicit_operator_flag(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    harness.stamp.write_text("linux-firmware-20260301\n", encoding="utf-8")

    refused = harness.run()

    assert refused.returncode != 0
    assert "Refusing possible rollback" in refused.stderr
    _assert_original_firmware(harness)
    assert not harness.backup.exists()

    allowed = harness.run("--allow-downgrade")

    assert allowed.returncode == 0, allowed.stderr
    assert harness.stamp.read_text(encoding="utf-8").strip() == harness.release


def test_invalid_threshold_fails_before_network_or_filesystem_changes(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run(REQUIRED_WORK_MB="not-a-number")

    assert result.returncode != 0
    assert "REQUIRED_WORK_MB must be a non-negative integer" in result.stderr
    assert not harness.call_log.exists()
    assert not harness.backup.exists()
    _assert_original_firmware(harness)


def test_non_linux_host_is_rejected_before_firmware_changes(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    harness._write_command("uname", "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n")

    result = harness.run()

    assert result.returncode == 1
    assert "require Linux" in result.stderr
    assert not harness.call_log.exists()
    _assert_original_firmware(harness)


def test_overlapping_backup_path_is_rejected(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run(BACKUP_DIR=str(harness.firmware / "backup"))

    assert result.returncode != 0
    assert "backup path must not equal or sit inside firmware path" in result.stderr
    _assert_original_firmware(harness)


def _crash_install(harness: UpdaterHarness, *arguments: str, phase="install") -> None:
    result = harness.run(*arguments, CRASH_AT=phase)
    assert result.returncode == -9, result.stderr
    assert harness.recovery.is_file()
    assert harness.backup.is_dir()


def test_sigkill_install_recovers_original_firmware_before_network(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)
    assert (harness.firmware / "vendor" / "device.bin.zst").exists()

    retried = harness.run(FAIL_NETWORK="1", BACKUP_DIR=str(tmp_path / "next-backup"))

    assert retried.returncode == 1
    assert "Automatic rollback completed" in retried.stderr
    _assert_original_firmware(harness)
    assert harness.stamp.read_text().strip() == harness.installed_release
    assert not harness.pending.exists()
    assert not harness.recovery.exists()
    assert not harness.backup.exists()


def test_sigkill_after_stamp_before_journal_commit_rolls_back(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness, phase="before_commit")
    assert harness.stamp.read_text().strip() == harness.release

    retried = harness.run(FAIL_NETWORK="1")

    assert retried.returncode == 1
    _assert_original_firmware(harness)
    assert harness.stamp.read_text().strip() == harness.installed_release
    assert not harness.recovery.exists()


def test_sigkill_during_same_release_force_install_still_rolls_back(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    harness.stamp.write_text(harness.release + "\n")
    _crash_install(harness, "--force")

    retried = harness.run()

    assert retried.returncode == 0, retried.stderr
    _assert_original_firmware(harness)
    assert "nothing to do" in retried.stdout
    assert not harness.recovery.exists()


def test_repeated_sigkill_during_rollback_keeps_backup_until_durable_completion(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)

    interrupted_rollback = harness.run(CRASH_AT="rollback")

    assert interrupted_rollback.returncode == -9
    assert harness.backup.is_dir()
    assert harness.recovery.is_file()
    retried = harness.run(FAIL_NETWORK="1")
    assert retried.returncode == 1
    _assert_original_firmware(harness)
    assert not harness.recovery.exists()
    assert not harness.backup.exists()


def test_committed_journal_recovers_pending_initramfs_without_reinstall(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness, phase="committed")

    retried = harness.run()

    assert retried.returncode == 0, retried.stderr
    assert "Pending initramfs rebuild completed" in retried.stdout
    assert "nothing to do" in retried.stdout
    assert (harness.firmware / "vendor" / "device.bin.zst").exists()
    assert harness.backup.is_dir()
    assert not harness.recovery.exists()
    assert not harness.pending.exists()


def test_recovery_restores_absent_previous_stamp(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    harness.stamp.unlink()
    _crash_install(harness)

    retried = harness.run(FAIL_NETWORK="1")

    assert retried.returncode == 1
    _assert_original_firmware(harness)
    assert not harness.stamp.exists()
    assert not harness.recovery.exists()


def test_recovery_rejects_replaced_backup_without_deleting_firmware(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)
    harness.backup.rename(tmp_path / "real-backup")
    shutil.copytree(tmp_path / "real-backup", harness.backup)
    (harness.firmware / "keep-me").write_text("still needed")

    retried = harness.run()

    assert retried.returncode == 1
    assert "backup identity changed" in retried.stderr
    assert (harness.firmware / "keep-me").exists()
    assert harness.recovery.exists()


def test_recovery_rejects_mismatched_configured_state_paths(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)

    retried = harness.run(PENDING_INITRAMFS_FILE=str(tmp_path / "other.pending"))

    assert retried.returncode == 1
    assert "configured paths disagree" in retried.stderr
    assert harness.recovery.exists()
    assert (harness.firmware / "vendor" / "device.bin.zst").exists()


def test_recovery_rejects_unprotected_or_malformed_journal(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)
    harness.recovery.chmod(0o666)
    unprotected = harness.run()
    assert unprotected.returncode == 1
    assert "protected root-owned" in unprotected.stderr
    harness.recovery.chmod(0o644)
    harness.recovery.write_bytes(harness.recovery.read_bytes()[:-1])
    malformed = harness.run()
    assert malformed.returncode == 1
    assert "invalid recovery journal" in malformed.stderr
    assert harness.backup.exists()


def test_recovery_journal_requires_protected_parent_and_distinct_external_paths(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)
    harness.state.chmod(0o777)
    unprotected = harness.run()
    assert unprotected.returncode == 1
    assert "root-owned parent" in unprotected.stderr
    harness.state.chmod(0o755)
    internal = harness.run(STAMP_FILE=str(harness.firmware / "release.version"))
    assert internal.returncode == 1
    assert "outside firmware and backup trees" in internal.stderr
    assert not harness.call_log.exists()


@pytest.mark.parametrize("tamper", ["owner", "hardlink", "symlink", "root-backup"])
def test_recovery_rejects_untrusted_journal_before_touching_firmware(tmp_path: Path, tamper: str) -> None:
    harness = UpdaterHarness(tmp_path)
    _crash_install(harness)
    overrides = {}
    if tamper == "owner":
        overrides["FAKE_UNTRUSTED_PATH"] = str(harness.recovery)
    elif tamper == "hardlink":
        os.link(harness.recovery, tmp_path / "second-link")
    elif tamper == "symlink":
        target = tmp_path / "journal-target"
        harness.recovery.rename(target)
        harness.recovery.symlink_to(target)
    else:
        fields = harness.recovery.read_bytes().split(b"\0")
        fields[3] = b"/"
        harness.recovery.write_bytes(b"\0".join(fields))

    retried = harness.run(**overrides)

    assert retried.returncode == 1
    assert (harness.firmware / "vendor" / "device.bin.zst").exists()
    assert harness.backup.exists()


def test_install_durability_barriers_precede_phase_commit(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run()

    assert result.returncode == 0, result.stderr
    events = [line.split("|") for line in harness.sync_log.read_text().splitlines()]
    backup = next(i for i, event in enumerate(events) if event[0] == str(harness.backup))
    prepared = next(i for i, event in enumerate(events) if event[1] == "installing")
    firmware = next(i for i, event in enumerate(events) if event[0] == str(harness.firmware))
    stamp = next(i for i, event in enumerate(events) if event[2] == harness.release)
    committed = next(i for i, event in enumerate(events) if event[1] == "committed")
    assert backup < prepared < firmware < stamp < committed
    assert not harness.recovery.exists()


def test_failed_commit_sync_rewrites_rollback_intent_before_restoring(tmp_path: Path) -> None:
    harness = UpdaterHarness(tmp_path)

    result = harness.run(FAIL_COMMIT_SYNC_ONCE_FILE=str(tmp_path / "sync-failed"))

    assert result.returncode == 1
    assert "could not durably commit installation" in result.stderr
    _assert_original_firmware(harness)
    assert harness.stamp.read_text().strip() == harness.installed_release
    assert not harness.recovery.exists()
    events = [line.split("|") for line in harness.sync_log.read_text().splitlines()]
    committed = next(i for i, event in enumerate(events) if event[1] == "committed")
    rollback = events[committed + 1:]
    rewritten = next(i for i, event in enumerate(rollback) if event[1] == "installing")
    restored = next(i for i, event in enumerate(rollback) if event[0] == str(harness.firmware))
    assert rewritten < restored
