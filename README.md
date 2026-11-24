# visual-change-ocr-monitor
Real-time hybrid web monitor and two-pass OCR automation pipeline built with Python, Playwright, and RapidOCR for detecting visual asset changes and automating DOM interactions.
# Visual Change Monitor & Hybrid OCR Automation Pipeline

A real-time monitoring and browser automation tool built with **Python**, **Playwright**, and **RapidOCR (ONNX)**. Designed to detect newly added visual assets on dynamic web pages with minimal latency, extract embedded alphanumeric codes via a two-pass OCR pipeline, and automate resilient DOM form submissions.

## Key Architectural Features

- **Hybrid Data Acquisition:** Queries internal JSON endpoints directly within the authenticated browser context to minimize network latency and bypass fingerprint blocks, with an automatic fallback to Playwright network response interception.
- **Two-Pass OCR Engine:**
  - *Pass 1 (Fast Scan):* Direct inference on raw image bytes using `RapidOCR` (~150ms).
  - *Pass 2 (Enhanced Preprocessing):* Applies 2x Lanczos upscaling, grayscale conversion, and contrast enhancement via `Pillow` to capture low-contrast or stylized text.
- **Resilient DOM Interaction:** Filters responsive/hidden duplicate elements (`visible=true` locators) to reliably expand collapsed UI sections, populate inputs, and trigger form submissions.
- **Persistent Session Management:** Utilizes Playwright persistent browser contexts to maintain authenticated sessions across runs.

## Tech Stack

- **Language:** Python 3.12+
- **Browser Automation:** Playwright
- **OCR & Computer Vision:** RapidOCR (ONNX Runtime), Pillow (PIL)

## Setup & Usage

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
