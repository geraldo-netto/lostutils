# lostutils

Standalone Python CLI and Tk utilities for local file, link, hardware, and calendar-event workflows. Each root script is self-contained: scripts do not import helpers from sibling files in this repository.

There is no repository-wide requirements file. Install only the third-party packages needed by the script you run. Most CLI scripts support `--help`; `link_queue.py` supports `--help` and `--version` without opening its GUI.

## Quick reference

| Script | Purpose | Non-stdlib dependencies |
| --- | --- | --- |
| `bookmark-tidy.py` | Merge Chrome, Edge, Firefox, and Netscape bookmark exports or plain-text URL lists, dedupe URLs, preserve immutable folders, and recategorize mutable links with a local llama.cpp model. | Optional `llama-cpp-python` for categorization |
| `deduplicate-by-namev3.py` | Find near-duplicate text lines with batched Levenshtein distance. | `numpy`, `rapidfuzz` |
| `dedupl_numpy.py` | Fast duplicate-path extraction from a legacy fixed-width hash file. | `numpy` |
| `hash-recursive-ai5.py` | Recursively find duplicate files with staged BLAKE3 hashing. | `blake3` |
| `import_events.py` | Extract calendar events from `.ics`, text, image, and PDF files. | Optional extraction backends; see below. |
| `link_queue.py` | Tk GUI for routing pasted links to configured commands. | `pyyaml`; Tk bindings for Python |
| `minikeypad.py` | Tk GUI configurator for a MINI-KeyBoard USB keypad. | Optional `pyusb`; native `libusb` backend |
| `organize_by_extension.py` | Move files into extension buckets, using header sniffing by default. | None |
| `relocate_folder.py` | Copy a directory to another filesystem and replace the source with a symlink. | None |
| `remove-deduplv3.py` | Emit shell-safe `rm -f` commands for duplicate hash groups. | None |
| `update-linux-firmware.sh` | Install the latest signed linux-firmware release and rebuild initramfs. | Bash; system tools listed below |

## Safety notes

`organize_by_extension.py` and `relocate_folder.py` change the filesystem. Start with `--preview` or `--dry-run` respectively. `remove-deduplv3.py` prints commands but does not delete files by itself; review its output before running it through a shell. `update-linux-firmware.sh` performs privileged changes under `/lib/firmware`, writes release state under `/var/lib`, and rebuilds initramfs; read its backup and recovery notes before use.

## Duplicate and file utilities

### `hash-recursive-ai5.py`

Finds duplicate regular files under a directory and writes one line per file in each duplicate group:

```bash
python3 hash-recursive-ai5.py /path/to/tree
```

The output shape is `<digest> <path>`. Paths containing a line break, or the reserved `@lostutils-json:` prefix, use a tagged JSON representation so one filename cannot create forged records; `remove-deduplv3.py` decodes that representation before building commands. The script first groups by file size, then uses three BLAKE3 stages: head, sampled windows, and full-file verification. It is hardlink-aware and hashes one inode representative while still emitting aliases. Hash-dump output is disabled by default; enable it with `--hashes-file`.

Useful options include `--jobs`, `--quiet`, `--alias-cap`, `--hash-error-verbose-cap`, `--block-size`, and `--sample-size`. Quiet mode suppresses routine logs, advisory warnings, and the summary; hash errors, dump output, filesystem stall diagnostics, and SIGINT partial-results warnings remain visible. If a downstream pipe closes early, the command exits 1 without a traceback and finalizes any requested hash dump.

### `deduplicate-by-namev3.py`

Reads one string per line and emits near-duplicate pairs as `cleaned_a;cleaned_b;distance`:

```bash
python3 deduplicate-by-namev3.py names.txt --threshold 7 --workers -1
```

The script lowercases and normalizes each line before comparing. `--threshold` is the maximum Levenshtein distance to report, clamped internally to the `uint8` matrix limit. `--workers -1` uses all cores supported by RapidFuzz.

### `dedupl_numpy.py`

Processes a legacy hash file in a vectorized NumPy pass:

```bash
python3 dedupl_numpy.py hashes.txt
```

It groups records by a 32-byte hash at the start of each line, prints duplicate paths, then prints an `equal files:` summary.

Paths written in the tagged JSON form `@lostutils-json:<json>` (what `hash-recursive-ai5.py` emits for a filename that would not survive the `<hash> <path>` split) are decoded back to the real path, the same way `remove-deduplv3.py` decodes them. A path containing a newline cannot be written into a newline-separated stream, so it stays escaped and is reported on stderr; `--print0` (`-0`) separates records with NUL instead and emits those paths literally:

```bash
python3 dedupl_numpy.py --print0 hashes.txt | xargs -0 rm -f --
```

### `remove-deduplv3.py`

Reads a hash file where each line is `<hash><whitespace><path>` (including the tagged JSON path representation emitted for line-breaking filenames) and emits quoted `rm -f` commands for duplicates while keeping the entry with the longest basename:

```bash
python3 remove-deduplv3.py hashes.txt > remove-duplicates.sh
```

It auto-detects common Unicode BOMs, defaults to UTF-8 with `surrogateescape`, and accepts `--encoding` or `--strict` when needed. The script writes a summary to stderr.

## File organization and relocation

### `organize_by_extension.py`

Moves files under a root into extension buckets:

```bash
python3 organize_by_extension.py /path/to/root --preview
python3 organize_by_extension.py /path/to/root --threads 3
```

Bucket paths are shaped as `<extension>/<first-letter>00000/<filename>`, with up to 500 files per bucket. Header sniffing is enabled by default so files can be bucketed by detected type rather than by a misleading extension; pass `--no-sniff` to use filename extensions only. `--extra-zip-family` keeps declared ZIP-container extensions such as `usdz` or `xpi` from being bucketed as plain `zip`. `--prune-empty-dirs` removes empty directories left under the root after moves.

Exit codes: `0` every planned file was placed, `1` the run could not complete (unusable root or planning spool, stalled move stage, interrupt), `2` command-line error, `3` the run finished but skipped files or left a duplicate behind after a partial move.

Rerunning completes an interrupted hardlink move when its existing destination still names the same file. Private `.lostutils-remove-*` directories are reserved for source-removal recovery: scans leave them untouched and report their location for manual inspection.

### `relocate_folder.py`

Copies a directory to `<dest_root>/<source-basename>`, verifies the copy, then atomically replaces the original source directory with a symlink. POSIX-only: it preserves uid/gid through `os.chown` and opens the source `O_NOFOLLOW|O_DIRECTORY` to keep the swap safe from a symlink-substitution race, so on a host without those it refuses to start and exits `2`.

```bash
python3 relocate_folder.py ~/.cache /mnt/large-disk/apps --dry-run
python3 relocate_folder.py ~/.cache /mnt/large-disk/apps
```

By default verification checks file and directory presence, sizes, symlinks, and per-file SHA-256 before deleting the moved-aside original. Regular-file and directory extended attributes, including supported ACL attributes, are copied and checked before source removal; symlink extended attributes are not copied. `--no-checksum` uses size-only verification, and `--no-verify` skips post-copy verification; extended attributes are still checked when copied. Use `python3 relocate_folder.py <source> --recover` to restore an orphaned `<source>.relocate-backup` left by an interrupted swap. Abrupt termination during copying may leave incomplete data under `.relocate-copy-*`; inspect these private artifacts before removing them manually. Stop applications that hold files open under the source before running, or pass `--force` to skip the open-file precheck. The open-file precheck requires Linux `/proc`; when unavailable, the script warns and continues without checking for open files.

Three environment variables tune the run, each falling back to its default on an unparseable value: `RELOCATE_STALE_PID_WARN_AT` (default `1`) is the stale-PID count at which the open-file precheck warns that its snapshot is not authoritative — raise it on noisy hosts; `RELOCATE_DISK_SPACE_HEADROOM` (default `1.05`) is how much destination free space the pre-flight check demands relative to the payload, clamped at `1.0`; `RELOCATE_SHA256_RETRY_ATTEMPTS` (default `2`) is how many times a verify hash is retried when the read hits a transient truncation, clamped to at least `1`.

## Linux firmware maintenance

### `update-linux-firmware.sh`

Downloads the newest release tarball advertised by kernel.org, verifies its detached OpenPGP signature against the pinned linux-firmware signing fingerprint, stages the archive's `copy-firmware.sh` output, backs up the current firmware tree, installs the staged tree as `root:root`, removes stale compression variants, records the installed release, and rebuilds initramfs when a supported tool is available.

```bash
./update-linux-firmware.sh --help
./update-linux-firmware.sh
./update-linux-firmware.sh --force
./update-linux-firmware.sh --allow-downgrade
```

`--force` reinstalls the currently stamped release but does not weaken rollback protection. If the signed release advertised by the index is older than the installed root-owned stamp, the run is refused unless `--allow-downgrade` is supplied explicitly after the operator verifies that the downgrade is intentional. A root-owned pending marker is written before firmware changes and removed only after initramfs rebuild succeeds; if rebuilding fails, the next run retries it even when the release stamp is already current.

The script supports Linux systems with Bash and GNU userland features (`grep -P`, `df --output`, `find -printf`, `realpath`, `stat`, and `sort -V`). Required commands are `curl`, `flock`, `gpg`, `tar`, `rsync`, `sync` (GNU `sync -f`), and the decompressor matching the discovered release (`xz` or `gzip`). A non-root run also needs `sudo`. `zstd` is optional; without it firmware is staged uncompressed. Initramfs rebuilding supports Debian/Ubuntu-family `update-initramfs`, Fedora/RHEL-family `dracut`, and Arch-family `mkinitcpio`; if none is installed, the script completes with a warning and requires a manual rebuild for early-boot firmware.

Allow at least 5 GiB free under `/var/tmp` for the archive, uncompressed tar, and staging tree, plus 2 GiB on the `/lib/firmware` filesystem. The default backup is `/lib/firmware.backup.<timestamp>` and is never overwritten. Before installation the backup is checksum-verified against the live tree and its identity is pinned. A root-owned recovery journal at `${STAMP_FILE}.recovery` records the previous release, backup identity, and transaction phase before firmware changes. Its parent must be root-owned without group/other write access, and release/recovery state must reside outside the firmware and backup trees. Backup contents, journal/state changes, and installed firmware are flushed in commit order. Ordinary errors and catchable signals roll back immediately; after SIGKILL or power loss, startup validates the journal and restores the verified backup and previous stamp before network discovery or free-space checks. The backup stays intact until restoration and journal removal are durable, so an interrupted rollback can be retried. An explicitly committed journal preserves the installed firmware and pending initramfs retry, including same-release `--force` installs. If automatic rollback cannot verify or restore the backup, the script emits a `CRITICAL` message and manual recovery commands. If only the post-commit initramfs rebuild fails, the firmware remains installed and its pending marker is retained; do not restore the backup, but resolve the reported problem (often a full `/boot`) and rerun. Successful-install backups remain available until the operator deletes them after boot and hardware checks. The printed Bash restore procedure verifies the backup identity, invalidates the installed-release stamp before changing firmware, copies the retained backup, clears obsolete pending/recovery state, and rebuilds initramfs when supported. Commands stop at the first failure; retain the backup and retry after resolving that failure. Because manual restoration invalidates the stamp, a later updater run installs the advertised release again rather than incorrectly reporting that the older restored firmware is current.

Every runtime setting can be overridden through the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FIRMWARE_URL` | `https://www.kernel.org/pub/linux/kernel/firmware/` | Release index and download base URL. |
| `FW_DIR` | `/lib/firmware` | Installed firmware tree. |
| `STAMP_FILE` | `/var/lib/linux-firmware-release.version` | Installed release marker. |
| `PENDING_INITRAMFS_FILE` | `/var/lib/linux-firmware-initramfs.pending` | Root-owned state retained until the installed release has been incorporated into initramfs. |
| `OLD_GIT_STAMP` | `/var/lib/linux-firmware-git.commit` | Obsolete marker removed after a successful install. |
| `CACHE_DIR` | `/var/tmp/firmware-update-cache` | Persistent signature/archive cache; it must be a real directory owned by the invoking uid and is forced to mode `0700`. |
| `BACKUP_DIR` | `${FW_DIR}.backup.<timestamp>` | Backup destination; the path must not already exist. |
| `PINNED_FPR` | `4CDE8575E547BF835FE15807A31B6BD72486CFD6` | Trusted linux-firmware OpenPGP fingerprint. Verify any change independently against kernel.org. |
| `KEYSERVER` | `hkps://keyserver.ubuntu.com` | Source used only to retrieve key material matching `PINNED_FPR`. |
| `REQUIRED_WORK_MB` | `5120` | Minimum free MiB required on the work filesystem. |
| `REQUIRED_LIB_MB` | `2048` | Minimum free MiB required on the firmware filesystem. |
| `CURL_CONNECT_TIMEOUT` | `20` | Maximum seconds allowed to establish each curl connection. |
| `CURL_LOW_SPEED_LIMIT` | `1024` | Minimum acceptable curl transfer rate in bytes per second. |
| `CURL_LOW_SPEED_TIME` | `60` | Seconds below the minimum rate before curl aborts. |
| `GPG_KEYSERVER_TIMEOUT` | `30` | Maximum seconds allowed for keyserver retrieval. |

## Bookmark organization

### `bookmark-tidy.py`

Reads one or more bookmark files or folders, deduplicates normalized URLs, preserves immutable folder/category roots, asks a local llama.cpp model to categorize the remaining bookmarks, and writes a new bookmark file:

```bash
python3 bookmark-tidy.py ~/bookmarks --model /path/to/model.gguf -o tidy-bookmarks.json
python3 bookmark-tidy.py chrome-bookmarks.json firefox-places.sqlite --output-format netscape --immutable-root Work -o tidy-bookmarks.html
python3 bookmark-tidy.py urls.txt --model /path/to/model.gguf -o tidy-bookmarks.json
```

The default output format is Chrome bookmark JSON. `--output-format firefox` writes a Firefox backup-style JSON file, and `--output-format netscape` writes browser-importable Netscape HTML. UTF-8 `.txt` inputs accept either one absolute URL per line or multiple URLs separated by spaces, tabs, or newlines; every token must be a valid URL with a scheme, and spaces inside a URL must be percent-encoded as `%20`. If no input paths are supplied, the script looks for local Chrome, Edge, Chromium, and Firefox profile bookmarks. For duplicate matching, URL normalization strips fragments, default ports, tracking query parameters, `www.`, trailing slashes, and collapses `http`/`https` duplicates by default; each behavior has a matching `--keep-*` override. Output retains the selected bookmark's original URL. `llama-cpp-python` is loaded only when mutable bookmarks need categorization; pass `--auto-install-llama` to let the script install it with pip if it is missing. `--llm-inference-timeout` controls the per-request wait (default 120 seconds); a timeout restarts the worker for subsequent batches. If categorization reaches the consecutive-failure limit, fallback categories are still written, but the command exits 1.

The output destination and supplied model path are checked before categorization begins. Runs containing only immutable bookmarks do not require a model file. If some bookmark inputs cannot be read, valid bookmarks are still exported, an incomplete-import summary is shown, and the command exits 1.

## GUI tools

### `link_queue.py`

Opens a Tk GUI that routes pasted URLs to command templates by protocol:

```bash
python3 link_queue.py
```

Protocols can run immediately in background workers or enter a queue consumed with configurable delays and per-domain limits. Command templates support `{url}`, `{url_quoted}`, and `{protocol}` placeholders. Shell execution is configurable per protocol; the default path uses argv-style subprocess calls. A saved `protocols` mapping defines the complete handler set, including an empty mapping; deleted built-in handlers stay deleted after restart.

In POSIX shell mode, write `{url_quoted}` and `{protocol}` as standalone words without surrounding quotes, for example `curl -- {url_quoted}`. Values are passed as shell arguments. The editor and runner reject bare `{url}`, quoted or embedded placeholders, backticks, and here-documents; use `$(...)` for command substitution. Shell mode is unavailable on Windows.

Current config and queue state are YAML files named `link_queue_config.yaml` and `link_queue_state.yaml`, stored only in `$XDG_CONFIG_HOME/link_queue` or `~/.config/link_queue`. Files beside the script are ignored, including when the per-user directory is unavailable. To transfer an existing queue, review its command templates and manually copy its files into the per-user directory before starting. Legacy `link_queue_config.json` is migrated only from that directory.

If configuration loading fails, the GUI reports the error and uses defaults for the session while refusing to overwrite the original file. Repair the configuration and restart before saving settings.

### `minikeypad.py`

Opens a Tk configurator for a MINI-KeyBoard programmable USB keypad:

```bash
python3 minikeypad.py
python3 minikeypad.py --auto-install-pyusb
```

The supported device is VID `0x1189`, PID `0x8890`, HID interface `1`. The GUI runs without the device attached and reports a disconnected state. If `pyusb` is missing, the app starts without device access; installing it at startup is opt-in via `--auto-install-pyusb` or `MINIKEYPAD_AUTO_INSTALL=1`. `--no-auto-install` overrides both, so a shared environment can pin the behaviour off. A native `libusb` backend is still required from the operating system. On Linux, prefer a udev rule for device permissions instead of running the GUI as root.

## Calendar extraction

### `import_events.py`

Extracts calendar events from `.ics`, text, image, and PDF files into JSON, with optional combined `.ics` output:

```bash
python3 import_events.py ./events_data -o events.json
python3 import_events.py ./events_data -o events.json --emit-ics events.ics --recursive
```

By default it downloads and uses `ggml-org/Qwen2.5-VL-7B-Instruct-GGUF` with `Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf` and `mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf`. The model cache is selected from `IMPORT_EVENTS_CACHE_DIR`, then `$XDG_CACHE_HOME`, then `~/.cache/lostutils/import_events`.

Exit codes: `0` every file was processed, `1` some files failed extraction but the run covered them all, `2` the run did not cover every file (command-line or setup error, model unavailable, or workers abandoned mid-run). Codes `1` and `2` both still write whatever output was extracted.

Optional runtime dependencies enable richer extraction:

- `llama-cpp-python` loads the local GGUF language/vision models.
- `paddleocr` adds one OCR backend for images and rendered PDF pages.
- The `tesseract` executable adds a second OCR backend.
- `pypdf` extracts text from text-native PDFs.
- `PyMuPDF` (`fitz`) renders scanned PDFs for OCR/vision fallback.
- `icalendar` parses and writes iCalendar data.

Missing optional OCR/PDF dependencies degrade gracefully: the script logs the missing backend, uses the remaining stages, and exits non-zero only when a file extraction actually fails. PDF OCR is disabled by default; text-native PDFs use parsed text, while unreadable or scanned PDFs fall back to the vision model when the vision stack is available.

Language handling:

- `--language auto` detects language from available text before downstream stages. If no text exists before OCR, `--ocr-languages` tries a chain of PaddleOCR/Tesseract languages. The default chain is Brazilian Portuguese, English, Spanish, Italian, French, and German.
- `--ocr-language-score` controls whether OCR stops after the first language in the chain. At the default `0.70`, a confident first-language match skips the remaining OCR languages; otherwise the full chain is exhausted and OCR text is merged.
- `--ocr-fallback-language` remains available as a compatibility override and is tried first when explicitly set to a non-default language.
- Explicit `--language` values such as `en`, `pt`, `es`, `it`, `fr`, and `de` are passed through to OCR backends using their native language codes and are included in the LLM prompt.

Useful runtime knobs include `--llm-context`, `--max-content-chars`, `--max-ics-bytes`, `--max-image-bytes`, `--max-text-bytes`, `--llm-max-tokens`, `--llm-gpu-layers`, `--llm-main-gpu`, `--mlock`, `--ocr-engine`, `--ocr-languages`, `--ocr-language-score`, `--ocr-timeout`, `--tesseract-psm`, `--tesseract-path`, `--pdf-ocr-mode`, `--pdf-vision-pages`, `--pdf-vision-dpi`, `--stage-cache`, `--workers`, `--deterministic-order`, and `--summary-only`.

`--ocr-timeout` applies to the killable Tesseract subprocess only. PaddleOCR runs in-process and cannot be interrupted safely, so choose `--ocr-engine tesseract` when a hard per-call OCR deadline is required.

Untrusted inputs are size-bounded before they are read into memory: `--max-ics-bytes` (default 4 MB) and `--max-image-bytes` (default 128 MB) reject an oversize file, while `--max-text-bytes` (default 128 MB) caps how much of a text file is read before decoding. When `--max-content-chars` is omitted, the text budget is computed from the selected LLM context size. The default `--llm-context 0` lets llama.cpp use the model-native context window. The default `--llm-gpu-layers 0` uses CPU; pass a positive layer count or `-1` to opt into llama.cpp GPU offload. `--mlock` is opt-in and is skipped automatically when the model plus projector files would exceed 70% of the memory limit visible to the process.

## Tests and CI

GitHub Actions runs Ruff and Pyright against root scripts, then executes unit, regression, integration, and Hypothesis fuzz tests on Python 3.12 under Ubuntu 24.04, including Linux-only firmware and relocation checks. This full suite requires at least 80% global branch coverage and at least 80% statement coverage for every measured function or method.

Separate Windows Server 2025 and macOS 15 jobs run `python -m pytest -q tests/test_portable_contracts.py tests/test_repository_governance.py` with the same pinned Python and dependency lockfile. The portable contracts copy scripts into isolated directories, exercise CLI help and file transformations with spaces/Unicode paths, open and close both real Tk GUIs, and verify native lock contention and recovery after a killed owner. Guarded USB/kernel-driver and hard-link fallbacks also run without devices or downloads. Missing Tk/display support fails the GUI gate; on headless Linux, run this command through `xvfb-run -a`. Linux execution covers these contracts locally; native Windows/macOS results must come from their CI runners.

Before installing dependencies, CI runs the standalone `.github/check_governance.py` guard over Git-tracked text, including tests, documentation, and configuration. It rejects common provider tokens, private-key headers, literal credential assignments, and private home-directory paths. Diagnostics contain only repository-relative filename, line number, and rule; matched values are never printed. Binary assets are skipped, while invalid encodings in source/configuration files fail the check. This is a targeted literal guard, not a complete secret-history scanner. Intentional fixtures or generic examples require an explicit `.github/governance-allowlist.json` entry containing the exact repository-relative `path`, `rule`, SHA-256 of the full matched text as `sha256`, and a nonempty `reason`; exceptions for a different value/file/rule do not apply, and unused exceptions fail CI.

Run the same gates locally:

```bash
python3 .github/check_governance.py
python3 -m pip install -r requirements-ci.txt
ruff check *.py
pyright --pythonpath "$(command -v python3)" *.py
coverage run -m pytest -q
coverage report
coverage json
python3 tests/check_function_coverage.py coverage.json --minimum 80
```
