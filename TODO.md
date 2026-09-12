# TODO

## Open

### `bookmark-tidy.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `deduplicate-by-namev3.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `hash-recursive-ai5.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `link_queue.py`

| id | status | severity | effort | description |
|---|---|---|---|---|
| lq-plat-92 | open | medium | low | link_queue.py:5968 — [platform / UI / UX] Reject or disable Shell mode in the Windows protocol editor and context-menu toggle. The editor labels it `cmd.exe /c` and accepts `echo {url_quoted}` with shell=True, but `_spawn_shell_proc` always refuses shell=True on Windows, so a configuration accepted by the UI consumes the queued retry budget without launching. Reproduced the editor/runner mismatch with the Windows platform branch selected; make the UI explain the unsupported mode and cover both editor save and toggle. |

### `minikeypad.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `organize_by_extension.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `relocate_folder.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

### `remove-deduplv3.py`

| id | status | severity | effort | description |
|---|---|---|---|---|

## Blocked / Deferred

| id | status | severity | effort | description |
|---|---|---|---|---|

## Rejected / Won't fix

| id | status | severity | effort | description |
|---|---|---|---|---|
| bt-dead-50 | wont_fix | low | — | bookmark-tidy.py:1754-1758 — Keep the private helper's defensive missing-model guard; direct callers get a typed `UserError` even though the production caller already guards it. |
| dnv3-i18n-01 | wont_fix | low | — | deduplicate-by-namev3.py:54-62,162-261 — No translation catalog for this developer CLI unless localization becomes a product requirement; hard-coded English is accepted. |
| hr-plat-05 | rejected | low | — | hash-recursive-ai5.py:2028-2041 — Text-mode hash-dump offsets remain valid on Windows because `tell()`/`seek()` use compatible cookies and the patched fixed-width digest contains no newline. |
| lq-plat-02 | rejected | low | — | link_queue.py:209-234 — Keep inherited Windows profile ACL behavior; it supplies the write-integrity goal that POSIX mode `0700` provides without adding a `pywin32`/`icacls` dependency. |
| lq-plat-03 | rejected | low | — | link_queue.py:2485-2533 — Keep `start_new_session=True`; CPython accepts and ignores it on Windows, while process-tree shutdown uses `taskkill /T`. |
| mkp-i18n-01 | wont_fix | low | — | minikeypad.py:1045-1510 — No GUI translation catalog unless localization becomes a product requirement; hard-coded English is accepted. |
| rdv3-plat-01 | rejected | low | — | remove-deduplv3.py:1-24 — POSIX-shell output is the utility's explicit deliverable; adding Windows command dialects would expand scope and review risk. |
