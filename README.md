# lostutils

Standalone Python CLI and Tk utilities for local file, link, hardware, and calendar-event workflows. Each root script is self-contained: scripts do not import helpers from sibling files in this repository.

There is no repository-wide requirements file. Install only the third-party packages needed by the script you run. Most CLI scripts support `--help`; `link_queue.py` opens its GUI immediately.

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

The output shape is `<digest> <path>`. Paths containing a line break, or the reserved `@lostutils-json:` prefix, use a tagged JSON representation so one filename cannot create forged records; `remove-deduplv3.py` decodes that representation before building commands. The script first groups by file size, then hashes staged windows with BLAKE3: head, tail, center, and mid-file samples for large files. It is hardlink-aware and hashes one inode representative while still emitting aliases. Hash-dump output is disabled by default; enable it with `--hashes-file`.

Useful options include `--jobs`, `--quiet`, `--alias-cap`, `--hash-error-verbose-cap`, `--block-size`, and `--sample-size`.

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

### `relocate_folder.py`

Copies a directory to `<dest_root>/<source-basename>`, verifies the copy, then atomically replaces the original source directory with a symlink. POSIX-only: it preserves uid/gid through `os.chown` and opens the source `O_NOFOLLOW|O_DIRECTORY` to keep the swap safe from a symlink-substitution race, so on a host without those it refuses to start and exits `2`.

```bash
python3 relocate_folder.py ~/.cache /mnt/large-disk/apps --dry-run
python3 relocate_folder.py ~/.cache /mnt/large-disk/apps
```

By default verification checks file and directory presence, sizes, symlinks, and per-file SHA-256 before deleting the moved-aside original. `--no-checksum` uses size-only verification, and `--no-verify` skips post-copy verification entirely. Use `python3 relocate_folder.py <source> --recover` to restore an orphaned `<source>.relocate-backup` left by an interrupted swap. Stop applications that hold files open under the source before running, or pass `--force` to skip the open-file precheck.

Three environment variables tune the run, each falling back to its default on an unparseable value: `RELOCATE_STALE_PID_WARN_AT` (default `1`) is the stale-PID count at which the open-file precheck warns that its snapshot is not authoritative — raise it on noisy hosts; `RELOCATE_DISK_SPACE_HEADROOM` (default `1.05`) is how much destination free space the pre-flight check demands relative to the payload, clamped at `1.0`; `RELOCATE_SHA256_RETRY_ATTEMPTS` (default `2`) is how many times a verify hash is retried when the read hits a transient truncation, clamped to at least `1`.

## Linux firmware maintenance

### `update-linux-firmware.sh`

Downloads the newest release tarball advertised by kernel.org, verifies its detached OpenPGP signature against the pinned linux-firmware signing fingerprint, stages the archive's `copy-firmware.sh` output, backs up the current firmware tree, installs the staged tree as `root:root`, removes stale compression variants, records the installed release, and rebuilds initramfs when a supported tool is available.

```bash
./update-linux-firmware.sh --help
./update-linux-firmware.sh
./update-linux-firmware.sh --force
```

The script supports Linux systems with Bash and GNU userland features (`grep -P`, `df --output`, `find -printf`, and `sort -V`). Required commands are `curl`, `gpg`, `tar`, `rsync`, and the decompressor matching the discovered release (`xz` or `gzip`). A non-root run also needs `sudo`. `zstd` is optional; without it firmware is staged uncompressed. Initramfs rebuilding supports Debian/Ubuntu-family `update-initramfs`, Fedora/RHEL-family `dracut`, and Arch-family `mkinitcpio`; if none is installed, the script completes with a warning and requires a manual rebuild for early-boot firmware.

Allow at least 5 GiB free under `/var/tmp` for the archive, uncompressed tar, and staging tree, plus 2 GiB on the `/lib/firmware` filesystem. The default backup is `/lib/firmware.backup.<timestamp>` and is never overwritten. Backups are retained after success. If a failure occurs after backup creation but before the installed-release stamp is written, remove the incomplete firmware tree, move the backup back to `FW_DIR`, and rebuild initramfs with the detected tool. If only the post-install initramfs rebuild fails, do not restore the firmware backup; resolve the reported problem (often a full `/boot`) and rerun the displayed initramfs command. Do not delete the backup until the machine has booted and relevant hardware has been checked.

Every runtime setting can be overridden through the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FIRMWARE_URL` | `https://www.kernel.org/pub/linux/kernel/firmware/` | Release index and download base URL. |
| `FW_DIR` | `/lib/firmware` | Installed firmware tree. |
| `STAMP_FILE` | `/var/lib/linux-firmware-release.version` | Installed release marker. |
| `OLD_GIT_STAMP` | `/var/lib/linux-firmware-git.commit` | Obsolete marker removed after a successful install. |
| `CACHE_DIR` | `/var/tmp/firmware-update-cache` | Persistent signature/archive cache used for resumable downloads. |
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

The default output format is Chrome bookmark JSON. `--output-format firefox` writes a Firefox backup-style JSON file, and `--output-format netscape` writes browser-importable Netscape HTML. UTF-8 `.txt` inputs accept either one absolute URL per line or multiple URLs separated by spaces, tabs, or newlines; every token must be a valid URL with a scheme, and spaces inside a URL must be percent-encoded as `%20`. If no input paths are supplied, the script looks for local Chrome, Edge, Chromium, and Firefox profile bookmarks. URL normalization strips fragments, default ports, tracking query parameters, `www.`, trailing slashes, and collapses `http`/`https` duplicates by default; each behavior has a matching `--keep-*` override. `llama-cpp-python` is loaded only when mutable bookmarks need categorization; pass `--auto-install-llama` to let the script install it with pip if it is missing.

## GUI tools

### `link_queue.py`

Opens a Tk GUI that routes pasted URLs to command templates by protocol:

```bash
python3 link_queue.py
```

Protocols can run immediately in background workers or enter a queue consumed with configurable delays and per-domain limits. Command templates support `{url}`, `{url_quoted}`, and `{protocol}` placeholders. Shell execution is configurable per protocol; the default path uses argv-style subprocess calls.

Current config and queue state are YAML files named `link_queue_config.yaml` and `link_queue_state.yaml`. The preferred location is `$XDG_CONFIG_HOME/link_queue` or `~/.config/link_queue`; pre-existing files next to the script are reused so older in-flight queues are not orphaned. Legacy `link_queue_config.json` is read and migrated when present.

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

GitHub Actions runs Ruff and Pyright against root scripts, then executes unit, regression, integration, and Hypothesis fuzz tests on Python 3.12. CI requires at least 80% global branch coverage and at least 80% statement coverage for every measured function or method.

Run the same gates locally:

```bash
python3 -m pip install -r requirements-ci.txt
ruff check *.py
pyright --pythonpath "$(command -v python3)" *.py
coverage run -m pytest -q
coverage report
coverage json
python3 tests/check_function_coverage.py coverage.json --minimum 80
```
