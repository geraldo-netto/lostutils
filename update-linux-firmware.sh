#!/usr/bin/env bash
# Update /lib/firmware from the latest signed linux-firmware release tarball.
#
# Combines update-firmware.sh (release tarball + PGP verification, backup,
# space checks) with update-linux-firmware.sh (idempotent stamp, staged
# rsync install with root ownership, zstd compression, initramfs rebuild).
#
# Usage: update-linux-firmware.sh [--force]
#   Works as a regular user (sudo is used only for the install steps) or as
#   root (e.g. from cron/systemd). Signature trust comes from the pinned
#   fingerprint below, not from keyring contents, so either keyring works.
set -euo pipefail

# Every setting can be overridden via the environment (the test harness
# relies on this); the defaults are the real interface.
FIRMWARE_URL="${FIRMWARE_URL:-https://www.kernel.org/pub/linux/kernel/firmware/}"
FW_DIR="${FW_DIR:-/lib/firmware}"
STAMP_FILE="${STAMP_FILE:-/var/lib/linux-firmware-release.version}"
RECOVERY_FILE="${STAMP_FILE}.recovery"
PENDING_INITRAMFS_FILE="${PENDING_INITRAMFS_FILE:-/var/lib/linux-firmware-initramfs.pending}"
# Stamp left behind by the superseded update-linux-firmware.sh; removed on
# success so the two installers can't disagree about who manages FW_DIR
OLD_GIT_STAMP="${OLD_GIT_STAMP:-/var/lib/linux-firmware-git.commit}"
# Persistent so an interrupted download resumes across runs
CACHE_DIR="${CACHE_DIR:-/var/tmp/firmware-update-cache}"
BACKUP_DIR="${BACKUP_DIR:-${FW_DIR}.backup.$(date +%Y%m%d-%H%M%S)}"

# linux-firmware releases are signed by Josh Boyer <jwboyer@kernel.org>.
# Cross-check against https://www.kernel.org/signature.html before changing.
PINNED_FPR="${PINNED_FPR:-4CDE8575E547BF835FE15807A31B6BD72486CFD6}"
KEYSERVER="${KEYSERVER:-hkps://keyserver.ubuntu.com}"

# Peak on the work filesystem: tarball (~600MB, cache) + uncompressed tar
# (~1.6GB) + extracted tree (~1.6GB); staging replaces the tar afterwards
REQUIRED_WORK_MB="${REQUIRED_WORK_MB:-5120}"
REQUIRED_LIB_MB="${REQUIRED_LIB_MB:-2048}"
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-20}"
CURL_LOW_SPEED_LIMIT="${CURL_LOW_SPEED_LIMIT:-1024}"
CURL_LOW_SPEED_TIME="${CURL_LOW_SPEED_TIME:-60}"
GPG_KEYSERVER_TIMEOUT="${GPG_KEYSERVER_TIMEOUT:-30}"

FORCE=0
ALLOW_DOWNGRADE=0
usage() {
    echo "Usage: $0 [--force] [--allow-downgrade]"
}

for argument in "$@"; do
    case "$argument" in
        --force) FORCE=1 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1 ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            echo "Unknown argument: $argument" >&2
            exit 1
            ;;
    esac
done

# Firmware layout and syncfs durability barriers are Linux-specific.
if [ "$(uname -s)" != Linux ]; then
    echo "ERROR: firmware updates require Linux" >&2
    exit 1
fi

validate_space_threshold() {
    local name=$1
    local value=$2
    local normalized
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        echo "ERROR: $name must be a non-negative integer (got '$value')" >&2
        return 1
    fi
    normalized=$value
    while [[ "$normalized" == 0* && ${#normalized} -gt 1 ]]; do
        normalized=${normalized#0}
    done
    if [ "${#normalized}" -gt 10 ] \
            || { [ "${#normalized}" -eq 10 ] && ((10#$normalized > 2147483647)); }; then
        echo "ERROR: $name is too large for a portable MB threshold (got '$value')" >&2
        return 1
    fi
    printf -v "$name" '%s' "$normalized"
}

validate_space_threshold REQUIRED_WORK_MB "$REQUIRED_WORK_MB"
validate_space_threshold REQUIRED_LIB_MB "$REQUIRED_LIB_MB"

validate_positive_network_setting() {
    local name=$1
    local value=$2
    validate_space_threshold "$name" "$value"
    if [ "${!name}" -eq 0 ]; then
        echo "ERROR: $name must be greater than zero" >&2
        return 1
    fi
}

validate_positive_network_setting CURL_CONNECT_TIMEOUT "$CURL_CONNECT_TIMEOUT"
validate_positive_network_setting CURL_LOW_SPEED_LIMIT "$CURL_LOW_SPEED_LIMIT"
validate_positive_network_setting CURL_LOW_SPEED_TIME "$CURL_LOW_SPEED_TIME"
validate_positive_network_setting GPG_KEYSERVER_TIMEOUT "$GPG_KEYSERVER_TIMEOUT"
CURL_NETWORK_ARGS=(
    --connect-timeout "$CURL_CONNECT_TIMEOUT"
    --speed-limit "$CURL_LOW_SPEED_LIMIT"
    --speed-time "$CURL_LOW_SPEED_TIME"
)

RUN_UID=$(id -u)
if [ "$RUN_UID" -eq 0 ]; then
    SUDO=()
else
    SUDO=(sudo)
fi

ensure_private_cache() {
    if [ ! -e "$CACHE_DIR" ] && [ ! -L "$CACHE_DIR" ]; then
        if ! (umask 077; mkdir -p -- "$CACHE_DIR"); then
            echo "ERROR: could not create cache directory: $CACHE_DIR" >&2
            return 1
        fi
    fi
    if [ -L "$CACHE_DIR" ] || [ ! -d "$CACHE_DIR" ]; then
        echo "ERROR: cache path must be a real directory, not a symlink: $CACHE_DIR" >&2
        return 1
    fi
    local owner
    owner=$(stat -c '%u' -- "$CACHE_DIR")
    if [ "$owner" != "$RUN_UID" ]; then
        echo "ERROR: cache directory is owned by uid $owner, expected $RUN_UID: $CACHE_DIR" >&2
        return 1
    fi
    chmod 0700 -- "$CACHE_DIR"
    if [ "$(stat -c '%a' -- "$CACHE_DIR")" != 700 ]; then
        echo "ERROR: cache directory is not private mode 0700: $CACHE_DIR" >&2
        return 1
    fi
}

validate_cached_file() {
    local path=$1
    if [ -L "$path" ]; then
        echo "ERROR: refusing symlink at cache output: $path" >&2
        return 1
    fi
    if [ -e "$path" ]; then
        if [ ! -f "$path" ]; then
            echo "ERROR: cache output is not a regular file: $path" >&2
            return 1
        fi
        local owner links
        owner=$(stat -c '%u' -- "$path")
        links=$(stat -c '%h' -- "$path")
        if [ "$owner" != "$RUN_UID" ] || [ "$links" -ne 1 ]; then
            echo "ERROR: cache output must be owned by uid $RUN_UID with one link: $path" >&2
            return 1
        fi
    fi
}

validate_installed_stamp() {
    if [ -L "$STAMP_FILE" ]; then
        echo "ERROR: installed release stamp must not be a symlink: $STAMP_FILE" >&2
        return 1
    fi
    if [ -e "$STAMP_FILE" ]; then
        if [ ! -f "$STAMP_FILE" ]; then
            echo "ERROR: installed release stamp is not a regular file: $STAMP_FILE" >&2
            return 1
        fi
        if [ "$(stat -c '%u' -- "$STAMP_FILE")" -ne 0 ]; then
            echo "ERROR: installed release stamp is not owned by root: $STAMP_FILE" >&2
            return 1
        fi
    fi
}

validate_pending_initramfs_file() {
    if [ -L "$PENDING_INITRAMFS_FILE" ]; then
        echo "ERROR: pending initramfs marker must not be a symlink: $PENDING_INITRAMFS_FILE" >&2
        return 1
    fi
    if [ -e "$PENDING_INITRAMFS_FILE" ]; then
        if [ ! -f "$PENDING_INITRAMFS_FILE" ] \
                || [ "$(stat -c '%u' -- "$PENDING_INITRAMFS_FILE")" -ne 0 ]; then
            echo "ERROR: pending initramfs marker must be a root-owned regular file: $PENDING_INITRAMFS_FILE" >&2
            return 1
        fi
    fi
}

retry_pending_initramfs() {
    local pending_release=$1
    if [ "$pending_release" != "$INSTALLED" ]; then
        echo "ERROR: pending initramfs marker ($pending_release) disagrees with installed stamp ($INSTALLED)" >&2
        return 1
    fi
    if [ "${#INITRAMFS_CMD[@]}" -eq 0 ]; then
        echo "ERROR: initramfs rebuild is still pending for $pending_release, but no supported rebuild tool is installed" >&2
        return 1
    fi
    echo "Retrying pending initramfs rebuild (${INITRAMFS_CMD[*]})..."
    if ! "${SUDO[@]}" "${INITRAMFS_CMD[@]}"; then
        echo "ERROR: pending initramfs rebuild failed again; marker retained: $PENDING_INITRAMFS_FILE" >&2
        return 1
    fi
    remove_root_state "$PENDING_INITRAMFS_FILE"
    echo "Pending initramfs rebuild completed."
}

acquire_process_lock() {
    if ! exec {LOCK_FD}<"$CACHE_DIR"; then
        echo "ERROR: could not open cache directory for locking: $CACHE_DIR" >&2
        return 1
    fi
    if ! flock -n "$LOCK_FD"; then
        echo "ERROR: another firmware update is already running (lock: $CACHE_DIR)" >&2
        return 1
    fi
}

validate_rollback_paths() {
    FW_DIR=$(realpath -m -- "$FW_DIR")
    STAMP_FILE=$(realpath -m -s -- "$STAMP_FILE")
    PENDING_INITRAMFS_FILE=$(realpath -m -s -- "$PENDING_INITRAMFS_FILE")
    RECOVERY_FILE="${STAMP_FILE}.recovery"
    BACKUP_DIR=$(realpath -m -- "$BACKUP_DIR")
    if [ "$FW_DIR" = / ] || [ "$BACKUP_DIR" = / ]; then
        echo "ERROR: firmware and backup paths must not resolve to filesystem root" >&2
        return 1
    fi
    case "$BACKUP_DIR" in
        "$FW_DIR"|"$FW_DIR"/*)
            echo "ERROR: backup path must not equal or sit inside firmware path" >&2
            return 1
            ;;
    esac
    case "$FW_DIR" in
        "$BACKUP_DIR"/*)
            echo "ERROR: firmware path must not sit inside backup path" >&2
            return 1
            ;;
    esac
    validate_state_paths
}

validate_state_paths() {
    local path
    for path in "$STAMP_FILE" "$PENDING_INITRAMFS_FILE" "$RECOVERY_FILE"; do
        case "$path" in
            "$FW_DIR"|"$FW_DIR"/*|"$BACKUP_DIR"|"$BACKUP_DIR"/*)
                echo "ERROR: release/recovery state must be outside firmware and backup trees: $path" >&2
                return 1
                ;;
        esac
    done
    if [ "$STAMP_FILE" = "$PENDING_INITRAMFS_FILE" ] \
            || [ "$RECOVERY_FILE" = "$PENDING_INITRAMFS_FILE" ]; then
        echo "ERROR: release, pending, and recovery state paths must be distinct" >&2
        return 1
    fi
}

# --- Preflight ---------------------------------------------------------------

for cmd in curl flock gpg realpath stat sync tar rsync; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "ERROR: missing required command: $cmd" >&2
        exit 1
    fi
done

# Release discovery needs PCRE grep; without this check a grep failure below
# would be misreported as a kernel.org page-format change
if ! printf 'x' | grep -oP 'x' >/dev/null 2>&1; then
    echo "ERROR: grep with PCRE support (-P) is required" >&2
    exit 1
fi

# zstd lets the install match distro packaging (compressed firmware);
# without it we fall back to installing uncompressed files, which still works
ZSTD_ARGS=()
if command -v zstd >/dev/null 2>&1; then
    ZSTD_ARGS=(--zstd)
else
    echo "WARNING: zstd not found; firmware will be installed uncompressed"
fi

# Firmware loaded early in boot (GPU, some NICs) comes from the initramfs,
# so without a rebuild the update silently doesn't apply to those devices.
# -k all / --regenerate-all: rebuild for EVERY installed kernel, not just the
# newest — the user may be booted into an older, held-back kernel.
INITRAMFS_CMD=()
if command -v update-initramfs >/dev/null 2>&1; then
    INITRAMFS_CMD=(update-initramfs -u -k all)
elif command -v dracut >/dev/null 2>&1; then
    INITRAMFS_CMD=(dracut -f --regenerate-all)
elif command -v mkinitcpio >/dev/null 2>&1; then
    INITRAMFS_CMD=(mkinitcpio -P)
else
    echo "WARNING: no initramfs tool found (update-initramfs/dracut/mkinitcpio)."
    echo "Early-boot firmware (e.g. GPU) will NOT be updated until you rebuild"
    echo "the initramfs yourself."
fi

echo "=== Kernel Firmware Update (unified) ==="
echo "Kernel version: $(uname -r)"
echo ""

validate_rollback_paths
ensure_private_cache
acquire_process_lock

# Fail early if we can't get root, rather than mid-way through
if [ "${#SUDO[@]}" -gt 0 ]; then
    echo "Requesting sudo access (needed for backup and install)..."
    sudo -v
fi

WORK_DIR="$(mktemp -d /var/tmp/firmware-update.XXXXXX)"
TARBALL=""
SUCCESS=0
BACKUP_READY=0
BACKUP_ID=""
FW_MUTATION_STARTED=0
INSTALL_COMMITTED=0
PENDING_STATE_STARTED=0

backup_identity_matches() {
    [ -d "$BACKUP_DIR" ] \
        && [ ! -L "$BACKUP_DIR" ] \
        && [ "$(stat -c '%u' -- "$BACKUP_DIR" 2>/dev/null)" = 0 ] \
        && [ "$(stat -c '%d:%i' -- "$BACKUP_DIR" 2>/dev/null)" = "$BACKUP_ID" ]
}

print_restore_instructions() {
    echo "  sudo rm -rf $FW_DIR"
    echo "  sudo mv $BACKUP_DIR $FW_DIR"
    echo "  sudo rm -f $PENDING_INITRAMFS_FILE"
    if [ "${#INITRAMFS_CMD[@]}" -gt 0 ]; then
        echo "  sudo ${INITRAMFS_CMD[*]}"
    fi
}

restore_verified_backup() {
    echo "Automatic rollback: restoring verified backup $BACKUP_DIR" >&2
    if ! backup_identity_matches; then
        echo "CRITICAL: backup identity changed; refusing automatic restore." >&2
        print_restore_instructions >&2
        return 1
    fi
    # A failed commit sync may still have published the committed record.
    # Restore the installing phase durably before beginning any rollback.
    if ! write_recovery_record installing; then
        echo "CRITICAL: could not persist rollback intent; firmware and backup retained." >&2
        return 1
    fi
    # Keep the backup intact until rollback and its journal removal are durable:
    # a second crash during restoration must remain recoverable on the next run.
    if ! "${SUDO[@]}" rm -rf -- "$FW_DIR" \
            || ! "${SUDO[@]}" cp -a -- "$BACKUP_DIR" "$FW_DIR" \
            || ! "${SUDO[@]}" sync -f -- "$FW_DIR"; then
        echo "CRITICAL: automatic rollback failed; recovery journal and backup retained." >&2
        return 1
    fi
    if ! restore_previous_stamp \
            || ! remove_root_state "$PENDING_INITRAMFS_FILE" \
            || ! remove_root_state "$RECOVERY_FILE"; then
        echo "CRITICAL: firmware was restored, but recovery state could not be committed; rerun." >&2
        return 1
    fi
    FW_MUTATION_STARTED=0
    "${SUDO[@]}" rm -rf -- "$BACKUP_DIR"
    echo "Automatic rollback completed; original firmware restored." >&2
}

restore_previous_stamp() {
    if [ "$INSTALLED" = none ]; then
        remove_root_state "$STAMP_FILE"
    else
        write_root_state "$STAMP_FILE" "$INSTALLED"
    fi
}

write_root_stream() {
    local path=$1 temporary
    if ! temporary=$("${SUDO[@]}" mktemp "${path}.tmp.XXXXXX"); then
        return 1
    fi
    if ! "${SUDO[@]}" tee "$temporary" >/dev/null \
            || ! "${SUDO[@]}" chmod 0644 -- "$temporary" \
            || ! "${SUDO[@]}" sync -f -- "$temporary" \
            || ! "${SUDO[@]}" mv -fT -- "$temporary" "$path" \
            || ! "${SUDO[@]}" sync -f -- "$(dirname -- "$path")"; then
        "${SUDO[@]}" rm -f -- "$temporary"
        return 1
    fi
}

write_root_state() {
    printf '%s\n' "$2" | write_root_stream "$1"
}

remove_root_state() {
    "${SUDO[@]}" rm -f -- "$1" \
        && "${SUDO[@]}" sync -f -- "$(dirname -- "$1")"
}

root_path_is_protected() {
    local owner mode
    owner=$(stat -c '%u' -- "$1") || return 1
    mode=$(stat -c '%a' -- "$1") || return 1
    [[ "$owner" = 0 && "$mode" =~ ^[0-7]{3,4}$ ]] \
        && (( (8#$mode & 0022) == 0 ))
}

validate_recovery_location() {
    local parent
    parent=$(dirname -- "$RECOVERY_FILE")
    if [ -L "$parent" ] || [ ! -d "$parent" ] \
            || ! root_path_is_protected "$parent"; then
        echo "ERROR: recovery journal needs a root-owned parent without group/other write access: $parent" >&2
        return 1
    fi
    if [ -L "$RECOVERY_FILE" ]; then
        echo "ERROR: recovery journal must not be a symlink: $RECOVERY_FILE" >&2
        return 1
    fi
}

read_recovery_record() {
    local value=
    RECOVERY_FIELDS=()
    if [ ! -f "$RECOVERY_FILE" ] || ! root_path_is_protected "$RECOVERY_FILE" \
            || [ "$(stat -c '%h' -- "$RECOVERY_FILE")" -ne 1 ] \
            || [ "$(stat -c '%s' -- "$RECOVERY_FILE")" -gt 65536 ]; then
        echo "ERROR: recovery journal must be a protected root-owned regular file with one link and at most 64 KiB" >&2
        return 1
    fi
    while IFS= read -r -d '' value; do
        RECOVERY_FIELDS+=("$value")
        [ "${#RECOVERY_FIELDS[@]}" -le 9 ] || return 1
    done < "$RECOVERY_FILE"
    [ -z "$value" ] && [ "${#RECOVERY_FIELDS[@]}" -eq 9 ]
}

validate_recovery_record() {
    [ "${RECOVERY_FIELDS[0]}" = firmware-update-v1 ] \
        && [[ "${RECOVERY_FIELDS[1]}" =~ ^(installing|committed)$ ]] \
        && [ "${RECOVERY_FIELDS[2]}" = "$FW_DIR" ] \
        && [ "${RECOVERY_FIELDS[5]}" = "$STAMP_FILE" ] \
        && [ "${RECOVERY_FIELDS[6]}" = "$PENDING_INITRAMFS_FILE" ] \
        && [[ "${RECOVERY_FIELDS[4]}" =~ ^[0-9]+:[0-9]+$ ]] \
        && [[ "${RECOVERY_FIELDS[7]}" =~ ^(none|linux-firmware-[0-9]+)$ ]] \
        && [[ "${RECOVERY_FIELDS[8]}" =~ ^linux-firmware-[0-9]+$ ]]
}

write_recovery_record() {
    printf '%s\0' firmware-update-v1 "$1" "$FW_DIR" "$BACKUP_DIR" "$BACKUP_ID" \
        "$STAMP_FILE" "$PENDING_INITRAMFS_FILE" "$INSTALLED" "$RELEASE" \
        | write_root_stream "$RECOVERY_FILE"
}

recover_interrupted_install() {
    # Isolate recorded paths/state from the next requested installation.
    local BACKUP_DIR BACKUP_ID INSTALLED RELEASE
    local FW_DIR="$FW_DIR" STAMP_FILE="$STAMP_FILE"
    local PENDING_INITRAMFS_FILE="$PENDING_INITRAMFS_FILE" RECOVERY_FILE="$RECOVERY_FILE"
    local FW_MUTATION_STARTED=0
    local -a RECOVERY_FIELDS
    validate_recovery_location || return 1
    [ -e "$RECOVERY_FILE" ] || return 0
    if ! read_recovery_record || ! validate_recovery_record; then
        echo "ERROR: invalid recovery journal or configured paths disagree: $RECOVERY_FILE" >&2
        return 1
    fi
    BACKUP_DIR=${RECOVERY_FIELDS[3]}
    BACKUP_ID=${RECOVERY_FIELDS[4]}
    INSTALLED=${RECOVERY_FIELDS[7]}
    RELEASE=${RECOVERY_FIELDS[8]}
    validate_rollback_paths || return 1
    if [ "$BACKUP_DIR" != "${RECOVERY_FIELDS[3]}" ]; then
        echo "ERROR: recorded backup path has changed; refusing recovery" >&2
        return 1
    fi
    validate_installed_stamp && validate_pending_initramfs_file || return 1
    if [ "${RECOVERY_FIELDS[1]}" = committed ]; then
        finish_committed_recovery
    else
        restore_verified_backup
    fi
}

finish_committed_recovery() {
    if [ "$(cat "$STAMP_FILE" 2>/dev/null)" != "$RELEASE" ]; then
        echo "ERROR: committed journal disagrees with installed stamp; inspect recovery state" >&2
        return 1
    fi
    remove_root_state "$RECOVERY_FILE"
}

cleanup() {
    local status=$?
    set +e
    trap - EXIT INT TERM HUP
    if [ "$FW_MUTATION_STARTED" -eq 1 ] && [ "$INSTALL_COMMITTED" -ne 1 ] \
            && [ "$BACKUP_READY" -eq 1 ]; then
        restore_verified_backup
    elif [ "$PENDING_STATE_STARTED" -eq 1 ] && [ "$INSTALL_COMMITTED" -ne 1 ]; then
        "${SUDO[@]}" rm -f -- "$PENDING_INITRAMFS_FILE"
    fi
    if [ "$SUCCESS" -eq 1 ]; then
        rm -rf "$WORK_DIR"
    else
        # Keep only logs: the extracted tree and staging are derivable from
        # the cached tarball, and stranding ~3GB per failed run fills /var/tmp
        rm -rf "$WORK_DIR/fw.tar" "$WORK_DIR"/linux-firmware-* "$WORK_DIR/staging"
        echo ""
        echo "Script did not complete."
        if [ -n "${TARBALL:-}" ] && [ -f "${TARBALL:-}" ]; then
            echo "Download kept for resume: $TARBALL"
        fi
        echo "Logs kept in: $WORK_DIR"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

recover_interrupted_install

# A failing df must fail the check, not silently skip it: an empty result
# would make '[ "" -lt N ]' exit 2, which an if-condition treats as false
avail_mb() {
    local out
    out=$(df --output=avail -BM -- "$1" 2>/dev/null | tail -1 | tr -dc '0-9') || true
    if [ -z "$out" ]; then
        echo "ERROR: could not determine free space for $1" >&2
        return 1
    fi
    printf '%s\n' "$out"
}
WORK_AVAIL=$(avail_mb "$WORK_DIR")
LIB_AVAIL=$(avail_mb "$FW_DIR")
if [ "$WORK_AVAIL" -lt "$REQUIRED_WORK_MB" ]; then
    echo "ERROR: Less than ${REQUIRED_WORK_MB}MB free in $WORK_DIR" >&2
    exit 1
fi
if [ "$LIB_AVAIL" -lt "$REQUIRED_LIB_MB" ]; then
    echo "ERROR: Less than ${REQUIRED_LIB_MB}MB free on the $FW_DIR filesystem" >&2
    exit 1
fi

cd "$WORK_DIR"

# --- Discover latest release -------------------------------------------------

echo "[1/7] Fetching latest firmware list..."
if ! INDEX_HTML=$(curl -fsS "${CURL_NETWORK_ARGS[@]}" "$FIRMWARE_URL"); then
    echo "ERROR: failed to fetch release index from $FIRMWARE_URL" >&2
    exit 1
fi
# "|| true" scoped to the pipeline AFTER a successful fetch: a no-match grep
# must not abort under set -e/pipefail before the -z check prints its error
LATEST_FIRMWARE=$(printf '%s\n' "$INDEX_HTML" \
    | grep -oP 'href="\Klinux-firmware-[0-9]+\.tar\.(?:xz|gz)' \
    | sort -V | tail -1 || true)

if [ -z "$LATEST_FIRMWARE" ]; then
    echo "ERROR: Could not determine latest firmware filename" >&2
    echo "(the index was fetched OK — kernel.org page format may have changed)" >&2
    exit 1
fi
case "$LATEST_FIRMWARE" in
    *.tar.xz) DECOMPRESSOR=xz ;;
    *.tar.gz) DECOMPRESSOR=gzip ;;
    *)
        echo "ERROR: Unknown compression format: $LATEST_FIRMWARE" >&2
        exit 1
        ;;
esac
if ! command -v "$DECOMPRESSOR" >/dev/null 2>&1; then
    echo "ERROR: missing required decompressor for $LATEST_FIRMWARE: $DECOMPRESSOR" >&2
    exit 1
fi
# Stamp stores the release name without compression extension so a .gz/.xz
# variant flip on kernel.org's index can't force a spurious reinstall
RELEASE="${LATEST_FIRMWARE%.tar.*}"
echo "Latest firmware: $RELEASE"

validate_installed_stamp
INSTALLED=$(cat "$STAMP_FILE" 2>/dev/null || echo none)
if [ "$INSTALLED" != none ] \
        && [[ ! "$INSTALLED" =~ ^linux-firmware-[0-9]+$ ]]; then
    echo "ERROR: installed release stamp is malformed: $STAMP_FILE" >&2
    echo "Refusing to weaken rollback protection; inspect the stamp manually." >&2
    exit 1
fi
validate_pending_initramfs_file
PENDING_INITRAMFS=$(cat "$PENDING_INITRAMFS_FILE" 2>/dev/null || echo none)
if [ "$PENDING_INITRAMFS" != none ]; then
    if [[ ! "$PENDING_INITRAMFS" =~ ^linux-firmware-[0-9]+$ ]]; then
        echo "ERROR: pending initramfs marker is malformed: $PENDING_INITRAMFS_FILE" >&2
        exit 1
    fi
    retry_pending_initramfs "$PENDING_INITRAMFS"
fi
if [ "$INSTALLED" != none ] && [ "$RELEASE" != "$INSTALLED" ] \
        && [ "$(printf '%s\n%s\n' "$RELEASE" "$INSTALLED" | sort -V | head -1)" = "$RELEASE" ] \
        && [ "$ALLOW_DOWNGRADE" -ne 1 ]; then
    echo "ERROR: discovered release $RELEASE is older than installed $INSTALLED" >&2
    echo "Refusing possible rollback; use --allow-downgrade only after verifying intent." >&2
    exit 1
fi
if [ "$RELEASE" = "$INSTALLED" ] && [ "$FORCE" -ne 1 ]; then
    echo "Already at $INSTALLED — nothing to do (use --force to reinstall)."
    SUCCESS=1
    exit 0
fi

# --- Signature and key first: fail in seconds, not after 600MB ---------------

echo "[2/7] Fetching signature and signer key..."
# kernel.org's .tar.sign signs the *uncompressed* tar. TLS alone only
# authenticates the CDN mirror; the PGP signature authenticates the release
# itself, which matters because staging runs copy-firmware.sh from it.
SIG_FILE="${RELEASE}.tar.sign"
SIG_PATH="$CACHE_DIR/$SIG_FILE"
validate_cached_file "$SIG_PATH"
if ! curl -fsS "${CURL_NETWORK_ARGS[@]}" --retry 3 \
        -o "$SIG_PATH" "${FIRMWARE_URL}${SIG_FILE}"; then
    echo "ERROR: Failed to download signature file $SIG_FILE" >&2
    exit 1
fi

# Importing by full fingerprint is safe: the pin is the trust anchor, the
# keyserver only supplies key material that must hash to it
if ! gpg --list-keys "$PINNED_FPR" >/dev/null 2>&1; then
    echo "Signer key not in keyring; importing $PINNED_FPR from $KEYSERVER..."
    if ! gpg --keyserver "$KEYSERVER" \
            --keyserver-options "timeout=$GPG_KEYSERVER_TIMEOUT" \
            --recv-keys "$PINNED_FPR"; then
        echo "ERROR: Could not import signing key $PINNED_FPR" >&2
        exit 1
    fi
fi

# --- Download ----------------------------------------------------------------

echo "[3/7] Downloading firmware (~600MB+, this may take a while)..."
TARBALL="$CACHE_DIR/$LATEST_FIRMWARE"
FW_TAR="$WORK_DIR/fw.tar"
validate_cached_file "$TARBALL"

# Decompressing once to a plain tar doubles as the integrity check and lets
# gpg/tar tf/tar xf below run without three more full decompressions
decompress_to_tar() {
    rm -f -- "$FW_TAR"
    "$DECOMPRESSOR" -cd -- "$TARBALL" > "$FW_TAR"
}

# The cache dir persists across runs, so -C - genuinely resumes an earlier
# interrupted run. (Within one run, curl's --retry restarts from this run's
# starting offset — curl truncates retried output, see curl issue #1084.)
if [ -f "$TARBALL" ] && decompress_to_tar 2>/dev/null; then
    echo "Using cached download: $TARBALL"
else
    if ! curl -fL "${CURL_NETWORK_ARGS[@]}" \
            --retry 5 --retry-all-errors -C - \
            -o "$TARBALL" "${FIRMWARE_URL}${LATEST_FIRMWARE}"; then
        echo "ERROR: Failed to download firmware" >&2
        echo "Partial download kept; rerun to resume: $TARBALL" >&2
        exit 1
    fi
    if ! decompress_to_tar; then
        echo "ERROR: Downloaded archive is corrupt; deleted. Rerun to refetch." >&2
        rm -f -- "$TARBALL"
        exit 1
    fi
fi
echo "Archive OK: $(du -h -- "$TARBALL" | cut -f1)"

# --- Verify ------------------------------------------------------------------

echo "[4/7] Verifying PGP signature..."
# --status-fd output is machine-readable; a VALIDSIG line carries the signing
# subkey's fingerprint and (last field) the primary key's fingerprint. Checking
# the pin, not just gpg's exit code, is the point: gpg exits 0 for a valid
# signature from ANY key in the keyring.
if ! GPG_STATUS=$(gpg --status-fd 1 --verify "$SIG_PATH" "$FW_TAR" 2>gpg-stderr.log); then
    cat gpg-stderr.log >&2
    echo "ERROR: PGP signature verification failed for $LATEST_FIRMWARE" >&2
    exit 1
fi

SIG_FPRS=$(printf '%s\n' "$GPG_STATUS" \
    | awk '/^\[GNUPG:\] VALIDSIG /{print $3; print $NF}' | sort -u)
if ! printf '%s\n' "$SIG_FPRS" | grep -qx "$PINNED_FPR"; then
    echo "ERROR: Signature is valid but from an UNEXPECTED key:" >&2
    printf '%s\n' "$SIG_FPRS" | sed 's/^/  /' >&2
    echo "Expected: $PINNED_FPR" >&2
    echo "Refusing to install. If the release key legitimately changed," >&2
    echo "verify the new one at https://www.kernel.org/signature.html and" >&2
    echo "update PINNED_FPR in this script." >&2
    exit 1
fi
echo "PGP signature OK (signed by pinned key $PINNED_FPR)"

# --- Extract and stage -------------------------------------------------------

echo "[5/7] Extracting and staging..."
# Determine the archive's top-level directory instead of guessing with ls.
# Listing goes to a file first: "tar tf | head" would SIGPIPE tar and
# abort the script under pipefail.
tar tf "$FW_TAR" > archive-list.txt
TOP_LEVEL_DIRS=$(cut -d/ -f1 archive-list.txt | LC_ALL=C sort -u)

# Every entry must live under one top-level directory: tar xf extracts
# the whole archive, so a second top-level path would escape the
# validated directory
if [ "$(printf '%s\n' "$TOP_LEVEL_DIRS" | wc -l)" -ne 1 ]; then
    echo "ERROR: Archive has multiple top-level entries:" >&2
    printf '%s\n' "$TOP_LEVEL_DIRS" >&2
    exit 1
fi
EXTRACTED_DIR=$TOP_LEVEL_DIRS

# Guard against "." (./-prefixed archives) or unexpected layouts
case "$EXTRACTED_DIR" in
    linux-firmware-*) ;;
    *)
        echo "ERROR: Unexpected archive top-level directory: '$EXTRACTED_DIR'" >&2
        exit 1
        ;;
esac

tar xf "$FW_TAR"
rm -f -- "$FW_TAR"
if [ ! -d "$EXTRACTED_DIR" ]; then
    echo "ERROR: Expected extracted directory $EXTRACTED_DIR not found" >&2
    exit 1
fi

# The tarball's installer copies only the firmware files listed in WHENCE and
# creates the alias symlinks a raw copy would miss. No raw-copy fallback: an
# archive without it isn't a layout this script understands, and blindly
# installing the whole checkout (Makefile, scripts, no alias links) would
# break devices while stamping the run as a success.
if [ ! -f "$WORK_DIR/$EXTRACTED_DIR/copy-firmware.sh" ]; then
    echo "ERROR: copy-firmware.sh not found in archive; refusing to install" >&2
    echo "an archive layout this script does not understand." >&2
    exit 1
fi

# Stage as the invoking user; ownership is normalized to root:root by
# rsync at install time, so nothing user-owned ever lands in FW_DIR
STAGING="$WORK_DIR/staging"
mkdir "$STAGING"
( cd "$WORK_DIR/$EXTRACTED_DIR" && sh ./copy-firmware.sh "${ZSTD_ARGS[@]}" "$STAGING" )

# copy-firmware.sh warns-and-ignores options it doesn't know, so verify the
# compression we asked for actually happened rather than stamping a silently
# uncompressed (and shadow-prone) install as success
if [ "${#ZSTD_ARGS[@]}" -gt 0 ] \
        && [ -z "$(find "$STAGING" -name '*.zst' -print -quit)" ]; then
    echo "ERROR: requested zstd compression but staging contains no .zst files;" >&2
    echo "copy-firmware.sh may have ignored --zstd (interface change?)" >&2
    exit 1
fi

# --- Backup ------------------------------------------------------------------

# The download can outlive sudo's cached credentials (default 15 min);
# refresh so the backup step doesn't hang on a hidden password prompt
if [ "${#SUDO[@]}" -gt 0 ]; then
    sudo -v
fi

if [ -e "$BACKUP_DIR" ] || [ -L "$BACKUP_DIR" ]; then
    echo "ERROR: Backup directory $BACKUP_DIR already exists" >&2
    exit 1
fi
echo "[6/7] Backing up current firmware to $BACKUP_DIR..."
if ! "${SUDO[@]}" cp -a "$FW_DIR" "$BACKUP_DIR"; then
    echo "ERROR: Backup failed, aborting before touching $FW_DIR" >&2
    exit 1
fi
if ! BACKUP_DIFFERENCES=$("${SUDO[@]}" rsync -aHnc --delete \
        --itemize-changes "$FW_DIR"/ "$BACKUP_DIR"/); then
    echo "ERROR: Backup verification failed, aborting before touching $FW_DIR" >&2
    exit 1
fi
if [ -n "$BACKUP_DIFFERENCES" ]; then
    echo "ERROR: Backup differs from $FW_DIR; refusing to install:" >&2
    printf '%s\n' "$BACKUP_DIFFERENCES" | head -20 >&2
    exit 1
fi
if [ -L "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ] \
        || [ "$(stat -c '%u' -- "$BACKUP_DIR")" -ne 0 ]; then
    echo "ERROR: verified backup is not a root-owned real directory: $BACKUP_DIR" >&2
    exit 1
fi
BACKUP_ID=$(stat -c '%d:%i' -- "$BACKUP_DIR")
BACKUP_READY=1
echo "Backup verified"

# --- Install -----------------------------------------------------------------

echo "[7/7] Installing (staging -> $FW_DIR, root:root, 0644/0755)..."
if ! "${SUDO[@]}" sync -f -- "$BACKUP_DIR" || ! write_recovery_record installing; then
    echo "ERROR: could not persist recovery journal; firmware was not modified" >&2
    exit 1
fi
PENDING_STATE_STARTED=1
if ! write_root_state "$PENDING_INITRAMFS_FILE" "$RELEASE"; then
    echo "ERROR: could not persist pending initramfs state; firmware was not modified" >&2
    exit 1
fi
FW_MUTATION_STARTED=1
if ! "${SUDO[@]}" rsync -rlt --force --stats \
        --chown=root:root \
        --chmod=Du=rwx,Dgo=rx,Fu=rw,Fgo=r \
        "$STAGING"/ "$FW_DIR"/; then
    echo "ERROR: Install failed. Restore with:" >&2
    print_restore_instructions >&2
    exit 1
fi

# The kernel firmware loader tries the plain filename before the .zst/.xz
# fallbacks, so a stale variant from an earlier install (e.g. an uncompressed
# file from the old update-firmware.sh) would shadow what we just installed
# and keep loading OLD firmware forever. Remove every variant of a staged
# file that staging itself doesn't provide.
( cd "$STAGING" && find . \( -type f -o -type l \) -printf '%P\n' ) \
    | LC_ALL=C sort > staged-files.txt
awk '{ b=$0; sub(/\.(zst|xz)$/,"",b); print b; print b".zst"; print b".xz" }' \
    staged-files.txt | LC_ALL=C sort -u > variants.txt
LC_ALL=C comm -23 variants.txt staged-files.txt > stale-candidates.txt
: > stale-remove.txt
while IFS= read -r rel; do
    if [ -e "$FW_DIR/$rel" ] || [ -L "$FW_DIR/$rel" ]; then
        printf '%s\0' "$FW_DIR/$rel" >> stale-remove.txt
    fi
done < stale-candidates.txt
if [ -s stale-remove.txt ]; then
    "${SUDO[@]}" xargs -0 rm -f -- < stale-remove.txt
    echo "Removed $(tr -cd '\0' < stale-remove.txt | wc -c) stale firmware variant(s) that would shadow this release"
fi

# Commit success before the initramfs rebuild: the firmware install itself is
# now complete, and a rebuild failure must not force a 600MB reinstall.
if ! "${SUDO[@]}" sync -f -- "$FW_DIR" || ! write_root_state "$STAMP_FILE" "$RELEASE"; then
    echo "ERROR: could not commit installed release stamp; rolling back" >&2
    exit 1
fi
if ! write_recovery_record committed; then
    echo "ERROR: could not durably commit installation; rolling back" >&2
    exit 1
fi
INSTALL_COMMITTED=1
remove_root_state "$RECOVERY_FILE"
"${SUDO[@]}" rm -f -- "$OLD_GIT_STAMP" 2>/dev/null || true

if [ "${#INITRAMFS_CMD[@]}" -gt 0 ]; then
    echo "Rebuilding initramfs (${INITRAMFS_CMD[*]})..."
    if ! "${SUDO[@]}" "${INITRAMFS_CMD[@]}"; then
        echo "ERROR: initramfs rebuild failed. The firmware install itself" >&2
        echo "succeeded and was recorded — do NOT restore the backup." >&2
        echo "Fix the underlying problem (often a full /boot) and rerun:" >&2
        echo "  sudo ${INITRAMFS_CMD[*]}" >&2
        exit 1
    fi
    remove_root_state "$PENDING_INITRAMFS_FILE"
fi

rm -f -- "$TARBALL" "$SIG_PATH"
SUCCESS=1
echo ""
echo "=== Update Complete: $RELEASE ==="
echo "Backup saved to: $BACKUP_DIR"
echo ""
echo "To restore from backup if needed:"
print_restore_instructions
if [ "${#INITRAMFS_CMD[@]}" -eq 0 ]; then
    echo ""
    echo "REMINDER: no initramfs tool was found, so early-boot firmware (e.g."
    echo "GPU) was NOT updated in the initramfs. Rebuild it manually."
fi
echo ""
echo "Reboot is recommended for changes to take effect:"
echo "  sudo reboot"
