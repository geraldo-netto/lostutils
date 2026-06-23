# lostutils
a set of cli/gui scripts/tools for a number of things

## import_events.py

`import_events.py` extracts calendar events from `.ics`, text, image, and PDF
files into JSON, with optional `.ics` output.

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
  stages. If no text exists before OCR, `--ocr-fallback-language` is used for
  PaddleOCR/Tesseract, and the LLM is told to detect the language from content
  or image input.
- Explicit `--language` values such as `en`, `pt`, `es`, `it`, `fr`, and `de`
  are passed through to OCR backends using their native language codes and are
  included in the LLM prompt.

Useful runtime knobs include `--llm-context`, `--max-content-chars`,
`--llm-max-tokens`, `--ocr-timeout`, `--tesseract-psm`,
`--pdf-vision-pages`, and `--pdf-vision-dpi`. When `--max-content-chars` is
omitted, the text budget is computed from the selected LLM context size.
