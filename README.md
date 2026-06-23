# lostutils
a set of cli/gui scripts/tools for a number of things

## import_events.py

`import_events.py` extracts calendar events from `.ics`, text, image, and PDF
files into JSON, with optional `.ics` output. By default it downloads and uses
`ggml-org/Qwen2.5-VL-7B-Instruct-GGUF` with the Q4_K_M language model and f16
mmproj projector.

Optional runtime dependencies enable richer extraction:

- `llama-cpp-python` loads the local GGUF language/vision models.
- `paddleocr` adds one OCR backend for images and rendered PDF pages.
- The `tesseract` executable adds a second OCR backend.
- `pypdf` extracts text from text-native PDFs.
- `PyMuPDF` (`fitz`) renders scanned PDFs for OCR/vision fallback.
- `icalendar` parses and writes iCalendar data.

Missing optional OCR/PDF dependencies degrade gracefully: the script logs the
missing backend, uses the remaining stages, and exits non-zero only when a file
extraction actually fails.

Language handling:

- `--language auto` detects language from available text before downstream
  stages. If no text exists before OCR, `--ocr-languages` tries a chain of
  PaddleOCR/Tesseract languages. The default chain is Brazilian Portuguese,
  English, Spanish, Italian, French, and German.
- `--ocr-language-score` controls whether OCR stops after the first language in
  the chain. At the default `0.70`, a confident first-language match skips the
  remaining OCR languages; otherwise the full chain is exhausted and the OCR
  text is merged. `--ocr-fallback-language` remains available as a compatibility
  override and is tried first when explicitly set to a non-default language.
- Explicit `--language` values such as `en`, `pt`, `es`, `it`, `fr`, and `de`
  are passed through to OCR backends using their native language codes and are
  included in the LLM prompt.

Useful runtime knobs include `--llm-context`, `--max-content-chars`,
`--llm-max-tokens`, `--ocr-languages`, `--ocr-language-score`,
`--ocr-timeout`, `--tesseract-psm`, `--pdf-vision-pages`, and
`--pdf-vision-dpi`. When `--max-content-chars` is omitted, the text budget is
computed from the selected LLM context size.
