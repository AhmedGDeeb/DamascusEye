# CLAUDE.md — ESP32-CAM Capture

Project context for Claude Code. User-facing docs (Arabic) are in README.md.

## What this is
ESP-IDF firmware for an **AI-Thinker ESP32-CAM** (OV2640, 4 MB flash, PSRAM):
- Capture a JPEG every `CONFIG_CAPTURE_INTERVAL_SEC` (default 60 s).
- Compare with the **last kept** image; keep the new one only if the scene changed.
- Queue kept images in PSRAM and upload them by HTTP POST to a server/database.
- Battery powered (18650 → TP4056 → MT3608 boost 5 V → 5V pin).

## Layout
```
CMakeLists.txt            project file
sdkconfig.defaults        PSRAM on, 4MB flash, single_app_large partition, cert bundle
main/main.c               all firmware logic (single file)
main/Kconfig.projbuild    user settings (menu "ESP32-CAM Capture Settings")
main/idf_component.yml    depends on espressif/esp32-camera (component registry)
server/upload_server.py   stdlib-only test receiver -> SQLite (images.db)
```

## Build / flash
- Developed with the VS Code ESP-IDF extension; verified to build cleanly on **ESP-IDF v5.4.1**
  and **v5.5.6** (installed at `C:/Espressif/frameworks/esp-idf-v5.5.6`, python env
  `idf5.5_py3.11_env`), with/without upload and preview. Not yet tested on real hardware.
- CLI: `idf.py set-target esp32 && idf.py menuconfig && idf.py build flash monitor`
- First build downloads `esp32-camera` from the component registry (needs internet).
- Flashing without the ESP32-CAM-MB board: hold BOOT (IO0→GND), tap RESET, release BOOT.

## Actual board in use: ESP32-S3 CAM (target `esp32s3`)
- Verified 2026-11-17 with esptool: ESP32-S3 rev v0.2, 8 MB embedded octal PSRAM, 16 MB flash.
- Two USB-C ports: **TTL** = CH340 → COM10 (flash + monitor, auto-reset works) and
  **OTG** = native USB (VID 303A). Use TTL.
- `sdkconfig.defaults.esp32s3` sets 16 MB flash + octal PSRAM. Pins in main.c under
  `#if CONFIG_IDF_TARGET_ESP32S3` (Freenove/ESP32-S3-EYE camera map);
  LEDs moved to GPIO 2 / 14 / 21 because 13/15 are camera pins.
- Flashed and running: camera + change detection OK (still scene → 0.0 %).
- WiFi mode choice `CAM_WIFI_AP` (default, for testing): board creates its own network
  `EdgeNode-CAM` / `12345678`, gallery at http://192.168.4.1/, no SNTP, no upload.
  `CAM_WIFI_STA`: joins a router (needed for upload). PC hotspot was not usable (reason 201).
- After editing Kconfig.projbuild run `idf.py reconfigure` — a plain build did not pick up new symbols.
- Local `sdkconfig` may hold WiFi credentials (do not copy them into sdkconfig.defaults).
  WiFi disconnect reason is logged (201 = AP not found, 15/204 = wrong password).

## Original design target: AI-Thinker ESP32-CAM — pin map (do not change casually)
| Function | GPIO | Notes |
|---|---|---|
| LED_STATUS  | 13 | blink = no WiFi, solid = connected |
| LED_CAPTURE | 14 | on during capture |
| LED_SAVED   | 15 | 2 blinks = kept, 5 fast = capture failed; all 3 blinking = fatal → restart |
| BOOT button | IO0 ↔ GND | hardware only; IO0 is camera XCLK at runtime → cannot be read as input |
| RESET button | EN ↔ GND | hardware only; EN not on header, soldered to RST button pad |

LEDs are wired GPIO → 330Ω → LED → GND (active high).

Constraints:
- GPIO 2/4/12/13/14/15 are shared with the SD card → **SD card is not used**. If SD is ever
  needed, use `SD_MMC` 1-bit mode (uses 2/14/15) and move LEDs.
- GPIO12 is a strapping pin (must be LOW at boot). GPIO16 = PSRAM, never use.
- GPIO4 = on-board flash LED. GPIO33 = on-board red LED (active low, not on header).
- Camera pins: standard AI-Thinker map (defined at top of main.c).

## Firmware design (main.c)
Tasks: `capture_task` (prio 5), `upload_task` (prio 4), `status_led_task` (prio 2).

Camera: JPEG, SVGA 800x600, quality 12, `fb_count=2`, `CAMERA_GRAB_LATEST`, FB in PSRAM.
One frame is discarded before each capture (avoid stale buffer).

Change detection:
1. `jpg2rgb565(..., JPG_SCALE_8X)` → 100x75 thumbnail → 8-bit luma.
2. Subtract global mean-brightness offset (ignore slow lighting changes).
3. Pixel "changed" if |diff| > `CONFIG_PIXEL_DIFF_THRESHOLD` (25).
4. Keep if changed area ≥ `CONFIG_CHANGE_PERCENT_X10` (20 = 2.0%).
5. Reference = last **kept** thumbnail (intentional, so slow drift eventually triggers a save).
First image after boot is always kept.

Upload:
- `jpeg_item_t {buf (PSRAM), len, ts}` in a FreeRTOS queue (`CONFIG_UPLOAD_QUEUE_LEN`, default 4).
- Full queue → drop oldest. Failed upload → keep item, retry every 10 s.
- Request: `POST CONFIG_UPLOAD_URL`, `Content-Type: image/jpeg`, headers
  `X-Device-Id`, `X-Timestamp` (unix s; 0 if SNTP not synced). HTTPS via `esp_crt_bundle_attach`.
- If `CONFIG_UPLOAD_ENABLE` is off, items are just logged and freed (placeholder).
- `upload_task` takes ownership with `xQueueReceive` (never peek: capture_task's drop-oldest
  would free a buffer mid-upload). On failure with a full queue, the in-flight item is dropped.

Preview (`CONFIG_PREVIEW_ENABLE`, default y): every kept image is also copied to a PSRAM ring of
`CONFIG_PREVIEW_COUNT` (default 8) and served by `esp_http_server` on port 80:
`/` gallery (newest first, time + change %), `/img?n=<seq>`, `/latest.jpg`. RAM only, for testing.

Note: esp32-camera ^2.1 decodes via `esp_jpeg` → `jpg2rgb565` output is **little-endian** RGB565.

Time: SNTP `pool.ntp.org`, TZ `UTC-3` (Damascus).
WiFi: STA, auto-reconnect, `WIFI_PS_MIN_MODEM`.

## Next steps (planned)
- Hook the upload to the real database backend (not chosen yet: Supabase / Firebase / own server).
- Power: consider deep sleep between captures for battery/solar life
  (would require storing the reference thumbnail in RTC memory or flash, ~7.5 KB).
- Possibly persist unsent images to flash/SD when offline for long periods.

## Conventions
- Keep code compatible with ESP-IDF ≥ 5.1.
- All tunables go in Kconfig.projbuild, not hard-coded.
- Verify with `idf.py build` after changes; keep it warning-free in `main/`.
