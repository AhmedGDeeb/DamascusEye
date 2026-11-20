/*
 * ESP32-CAM (AI-Thinker) - periodic capture with change detection
 *
 *  - Takes a photo every CONFIG_CAPTURE_INTERVAL_SEC (default 60 s)
 *  - Keeps the last kept photo in PSRAM as the reference
 *  - New photo is compared with the reference using an 8x down-scaled
 *    grayscale thumbnail; if there is no meaningful change it is discarded
 *  - Kept photos are queued for upload (HTTP POST, image/jpeg) to your
 *    server / database API
 *
 * Wiring (see README):
 *   LED_STATUS  -> GPIO13 (blinks = alive, solid = WiFi connected)
 *   LED_CAPTURE -> GPIO14 (on while taking a photo)
 *   LED_SAVED   -> GPIO15 (flashes when a new photo is kept)
 *   External RESET button: EN  <-> GND (hardware only, no code)
 *   External BOOT  button: IO0 <-> GND (hardware only, no code;
 *                          IO0 is the camera XCLK at run time)
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <inttypes.h>
#include <time.h>
#include <sys/time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/event_groups.h"

#include "esp_log.h"
#include "esp_err.h"
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_crt_bundle.h"

#include "esp_camera.h"
#include "img_converters.h"

static const char *TAG = "cam";

#if CONFIG_IDF_TARGET_ESP32S3
/* ---------------- Pins: ESP32-S3 CAM (N16R8, OTG + TTL USB-C) ----------------
 * Same camera map as Freenove ESP32-S3-WROOM CAM / ESP32-S3-EYE.
 * GPIO 13/15 are camera pins here, so the LEDs move. */
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK    15
#define CAM_PIN_SIOD     4
#define CAM_PIN_SIOC     5
#define CAM_PIN_D7      16
#define CAM_PIN_D6      17
#define CAM_PIN_D5      18
#define CAM_PIN_D4      12
#define CAM_PIN_D3      10
#define CAM_PIN_D2       8
#define CAM_PIN_D1       9
#define CAM_PIN_D0      11
#define CAM_PIN_VSYNC    6
#define CAM_PIN_HREF     7
#define CAM_PIN_PCLK    13

#define LED_STATUS   GPIO_NUM_2
#define LED_CAPTURE  GPIO_NUM_14
#define LED_SAVED    GPIO_NUM_21
#else
/* ---------------- Pins: AI-Thinker ESP32-CAM ---------------- */
#define CAM_PIN_PWDN    32
#define CAM_PIN_RESET   -1
#define CAM_PIN_XCLK     0
#define CAM_PIN_SIOD    26
#define CAM_PIN_SIOC    27
#define CAM_PIN_D7      35
#define CAM_PIN_D6      34
#define CAM_PIN_D5      39
#define CAM_PIN_D4      36
#define CAM_PIN_D3      21
#define CAM_PIN_D2      19
#define CAM_PIN_D1      18
#define CAM_PIN_D0       5
#define CAM_PIN_VSYNC   25
#define CAM_PIN_HREF    23
#define CAM_PIN_PCLK    22

#define LED_STATUS   GPIO_NUM_13
#define LED_CAPTURE  GPIO_NUM_14
#define LED_SAVED    GPIO_NUM_15
#endif

/* ---------------- Settings ---------------- */
#define FRAME_SIZE        FRAMESIZE_SVGA   /* 800x600 */
#define JPEG_QUALITY      12               /* 0-63, lower = better quality */
#define THUMB_MAX_W       200              /* enough for up to UXGA / 8 */
#define THUMB_MAX_H       150
#define THUMB_MAX_PIXELS  (THUMB_MAX_W * THUMB_MAX_H)

/* ---------------- Types / globals ---------------- */
typedef struct {
    uint8_t *buf;   /* JPEG data in PSRAM */
    size_t   len;
    time_t   ts;    /* capture time (unix seconds, 0 if not synced) */
} jpeg_item_t;

static QueueHandle_t       s_upload_q;
static EventGroupHandle_t  s_wifi_ev;
#define WIFI_CONNECTED_BIT BIT0

/* reference (last KEPT image) thumbnail */
static uint8_t *s_ref_gray;
static int      s_ref_w, s_ref_h;
static bool     s_has_ref = false;

/* scratch buffers */
static uint8_t *s_rgb565;
static uint8_t *s_new_gray;

/* ================================================================
 *  LEDs
 * ================================================================ */
static void leds_init(void)
{
    gpio_config_t io = {
        .pin_bit_mask = (1ULL << LED_STATUS) | (1ULL << LED_CAPTURE) | (1ULL << LED_SAVED),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    gpio_set_level(LED_STATUS, 0);
    gpio_set_level(LED_CAPTURE, 0);
    gpio_set_level(LED_SAVED, 0);
}

static void led_blink(gpio_num_t pin, int times, int ms)
{
    for (int i = 0; i < times; i++) {
        gpio_set_level(pin, 1);
        vTaskDelay(pdMS_TO_TICKS(ms));
        gpio_set_level(pin, 0);
        vTaskDelay(pdMS_TO_TICKS(ms));
    }
}

/* Status LED: slow blink = no WiFi, solid on = WiFi connected */
static void status_led_task(void *arg)
{
    while (1) {
        if (xEventGroupGetBits(s_wifi_ev) & WIFI_CONNECTED_BIT) {
            gpio_set_level(LED_STATUS, 1);
            vTaskDelay(pdMS_TO_TICKS(500));
        } else {
            gpio_set_level(LED_STATUS, 1);
            vTaskDelay(pdMS_TO_TICKS(100));
            gpio_set_level(LED_STATUS, 0);
            vTaskDelay(pdMS_TO_TICKS(900));
        }
    }
}

/* Fatal error: blink all LEDs then restart */
static void fatal_blink_restart(const char *why)
{
    ESP_LOGE(TAG, "FATAL: %s - restarting", why);
    for (int i = 0; i < 10; i++) {
        gpio_set_level(LED_STATUS, 1); gpio_set_level(LED_CAPTURE, 1); gpio_set_level(LED_SAVED, 1);
        vTaskDelay(pdMS_TO_TICKS(150));
        gpio_set_level(LED_STATUS, 0); gpio_set_level(LED_CAPTURE, 0); gpio_set_level(LED_SAVED, 0);
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    esp_restart();
}

/* ================================================================
 *  WiFi + time
 * ================================================================ */
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi_ev, WIFI_CONNECTED_BIT);
        /* 201 = AP not found (off / 5 GHz only), 15 / 204 = wrong password */
        ESP_LOGW(TAG, "WiFi disconnected (reason %d), retrying...",
                 ((wifi_event_sta_disconnected_t *)data)->reason);
        vTaskDelay(pdMS_TO_TICKS(2000));
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        ESP_LOGI(TAG, "Device joined %s -> open http://192.168.4.1/", CONFIG_DEVICE_ID);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "WiFi connected, IP " IPSTR, IP2STR(&e->ip_info.ip));
#if CONFIG_PREVIEW_ENABLE
        ESP_LOGI(TAG, "Preview page: http://" IPSTR "/", IP2STR(&e->ip_info.ip));
#endif
        xEventGroupSetBits(s_wifi_ev, WIFI_CONNECTED_BIT);
    }
}

static void wifi_init(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
#if CONFIG_CAM_WIFI_AP
    /* own network: phone/PC connects to the board, gallery at 192.168.4.1 */
    esp_netif_create_default_wifi_ap();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));

    wifi_config_t wc = {
        .ap = { .channel = 6, .max_connection = 4, .authmode = WIFI_AUTH_WPA2_PSK },
    };
    strncpy((char *)wc.ap.ssid, CONFIG_AP_SSID, sizeof(wc.ap.ssid) - 1);
    wc.ap.ssid_len = strlen((char *)wc.ap.ssid);
    strncpy((char *)wc.ap.password, CONFIG_AP_PASSWORD, sizeof(wc.ap.password) - 1);
    if (strlen(CONFIG_AP_PASSWORD) < 8) {
        ESP_LOGW(TAG, "AP password shorter than 8 chars -> open network");
        wc.ap.authmode = WIFI_AUTH_OPEN;
    }
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    xEventGroupSetBits(s_wifi_ev, WIFI_CONNECTED_BIT);   /* status LED solid = AP up */
    ESP_LOGI(TAG, "Own WiFi \"%s\" started -> connect and open http://192.168.4.1/", CONFIG_AP_SSID);
#else
    esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, CONFIG_WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, CONFIG_WIFI_PASSWORD, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* Modem sleep saves battery between uploads */
    esp_wifi_set_ps(WIFI_PS_MIN_MODEM);
#endif
}

#if CONFIG_CAM_WIFI_STA
static void time_init(void)
{
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&cfg);
    setenv("TZ", "UTC-3", 1);   /* Damascus, UTC+3 (POSIX sign is inverted) */
    tzset();
}
#endif

/* ================================================================
 *  Camera
 * ================================================================ */
static esp_err_t camera_init(void)
{
    camera_config_t c = {
        .pin_pwdn = CAM_PIN_PWDN,   .pin_reset = CAM_PIN_RESET, .pin_xclk = CAM_PIN_XCLK,
        .pin_sccb_sda = CAM_PIN_SIOD, .pin_sccb_scl = CAM_PIN_SIOC,
        .pin_d7 = CAM_PIN_D7, .pin_d6 = CAM_PIN_D6, .pin_d5 = CAM_PIN_D5, .pin_d4 = CAM_PIN_D4,
        .pin_d3 = CAM_PIN_D3, .pin_d2 = CAM_PIN_D2, .pin_d1 = CAM_PIN_D1, .pin_d0 = CAM_PIN_D0,
        .pin_vsync = CAM_PIN_VSYNC, .pin_href = CAM_PIN_HREF, .pin_pclk = CAM_PIN_PCLK,

        .xclk_freq_hz = 20000000,
        .ledc_timer   = LEDC_TIMER_0,
        .ledc_channel = LEDC_CHANNEL_0,

        .pixel_format = PIXFORMAT_JPEG,
        .frame_size   = FRAME_SIZE,
        .jpeg_quality = JPEG_QUALITY,
        .fb_count     = 2,
        .fb_location  = CAMERA_FB_IN_PSRAM,
        .grab_mode    = CAMERA_GRAB_LATEST,
    };
    esp_err_t err = esp_camera_init(&c);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Camera init failed: 0x%x", err);
        return err;
    }
    /* let auto-exposure / white balance settle */
    for (int i = 0; i < 5; i++) {
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    return ESP_OK;
}

/* ================================================================
 *  Change detection
 * ================================================================ */

/* JPEG -> 1/8 scale RGB565 -> 8-bit grayscale */
static bool jpeg_to_gray_thumb(const camera_fb_t *fb, uint8_t *gray, int *w, int *h)
{
    int tw = fb->width / 8, th = fb->height / 8;
    if (tw * th > THUMB_MAX_PIXELS) {
        ESP_LOGE(TAG, "Thumbnail too big (%dx%d)", tw, th);
        return false;
    }
    if (!jpg2rgb565(fb->buf, fb->len, s_rgb565, JPG_SCALE_8X)) {
        ESP_LOGE(TAG, "JPEG decode failed");
        return false;
    }
    for (int i = 0; i < tw * th; i++) {
        /* esp32-camera 2.1.x decodes via esp_jpeg: RGB565 is little-endian */
        uint16_t p = ((uint16_t)s_rgb565[2 * i + 1] << 8) | s_rgb565[2 * i];
        uint8_t r = (p >> 11) & 0x1F, g = (p >> 5) & 0x3F, b = p & 0x1F;
        /* scale to 0-255 and compute luma */
        gray[i] = (uint8_t)(((r << 3) * 77 + (g << 2) * 150 + (b << 3) * 29) >> 8);
    }
    *w = tw; *h = th;
    return true;
}

/* returns changed area in percent x10 (e.g. 25 = 2.5 %) */
static int compare_thumbs(const uint8_t *a, const uint8_t *b, int n)
{
    /* compensate a global brightness shift (light changes slowly / clouds) */
    int64_t sum_a = 0, sum_b = 0;
    for (int i = 0; i < n; i++) { sum_a += a[i]; sum_b += b[i]; }
    int offset = (int)((sum_b - sum_a) / n);

    int changed = 0;
    for (int i = 0; i < n; i++) {
        int d = (int)b[i] - (int)a[i] - offset;
        if (d < 0) d = -d;
        if (d > CONFIG_PIXEL_DIFF_THRESHOLD) changed++;
    }
    ESP_LOGI(TAG, "Brightness offset %d, changed pixels %d / %d", offset, changed, n);
    return (changed * 1000) / n;
}

/* ================================================================
 *  Upload (to your database / server API)
 * ================================================================ */
static void queue_for_upload(jpeg_item_t *item)
{
    if (xQueueSend(s_upload_q, item, 0) != pdTRUE) {
        /* queue full: drop the oldest image */
        jpeg_item_t old;
        if (xQueueReceive(s_upload_q, &old, 0) == pdTRUE) {
            ESP_LOGW(TAG, "Upload queue full, dropping oldest image");
            heap_caps_free(old.buf);
        }
        xQueueSend(s_upload_q, item, 0);
    }
}

#if CONFIG_UPLOAD_ENABLE
static esp_err_t http_upload(const jpeg_item_t *it)
{
    char ts_str[24];
    snprintf(ts_str, sizeof(ts_str), "%lld", (long long)it->ts);

    esp_http_client_config_t cfg = {
        .url = CONFIG_UPLOAD_URL,
        .method = HTTP_METHOD_POST,
        .timeout_ms = 15000,
        .crt_bundle_attach = esp_crt_bundle_attach,  /* used only for https:// */
    };
    esp_http_client_handle_t cli = esp_http_client_init(&cfg);
    esp_http_client_set_header(cli, "Content-Type", "image/jpeg");
    esp_http_client_set_header(cli, "X-Device-Id", CONFIG_DEVICE_ID);
    esp_http_client_set_header(cli, "X-Timestamp", ts_str);
    esp_http_client_set_post_field(cli, (const char *)it->buf, it->len);

    esp_err_t err = esp_http_client_perform(cli);
    int status = esp_http_client_get_status_code(cli);
    esp_http_client_cleanup(cli);

    if (err == ESP_OK && status >= 200 && status < 300) {
        ESP_LOGI(TAG, "Uploaded %u bytes, HTTP %d", (unsigned)it->len, status);
        return ESP_OK;
    }
    ESP_LOGW(TAG, "Upload failed: %s, HTTP %d", esp_err_to_name(err), status);
    return ESP_FAIL;
}
#endif

static void upload_task(void *arg)
{
    jpeg_item_t it;
    while (1) {
        /* take ownership of the oldest image, so capture_task's drop-oldest
         * can never free a buffer that is being uploaded */
        if (xQueueReceive(s_upload_q, &it, portMAX_DELAY) != pdTRUE) continue;
#if CONFIG_UPLOAD_ENABLE
        while (1) {
            xEventGroupWaitBits(s_wifi_ev, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
            if (http_upload(&it) == ESP_OK) break;
            if (uxQueueSpacesAvailable(s_upload_q) == 0) {
                /* queue full behind us: this one is the oldest, drop it */
                ESP_LOGW(TAG, "Upload queue full, dropping oldest image");
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(10000));   /* retry later, keep image */
        }
#else
        ESP_LOGI(TAG, "[upload disabled] image ready: %u bytes, ts=%lld",
                 (unsigned)it.len, (long long)it.ts);
#endif
        heap_caps_free(it.buf);
    }
}

/* ================================================================
 *  Preview: last kept images held temporarily in PSRAM and shown on
 *  http://<esp-ip>/ to check the change detection (nothing on flash)
 * ================================================================ */
#if CONFIG_PREVIEW_ENABLE
typedef struct {
    uint8_t *buf;
    size_t   len;
    time_t   ts;
    int      change_x10;   /* -1 = first image after boot */
    uint32_t seq;          /* 1, 2, 3 ... ; image k lives in slot (k-1) % N */
} preview_item_t;

static preview_item_t    s_prev[CONFIG_PREVIEW_COUNT];
static uint32_t          s_prev_seq;   /* number of images stored so far */
static SemaphoreHandle_t s_prev_mtx;

static void preview_store(const camera_fb_t *fb, time_t ts, int change_x10)
{
    uint8_t *buf = heap_caps_malloc(fb->len, MALLOC_CAP_SPIRAM);
    if (!buf) {
        ESP_LOGW(TAG, "No PSRAM for preview copy");
        return;
    }
    memcpy(buf, fb->buf, fb->len);
    /* don't stall the capture loop if a browser is slow downloading */
    if (xSemaphoreTake(s_prev_mtx, pdMS_TO_TICKS(3000)) != pdTRUE) {
        ESP_LOGW(TAG, "Preview busy, image not stored");
        heap_caps_free(buf);
        return;
    }
    preview_item_t *p = &s_prev[s_prev_seq % CONFIG_PREVIEW_COUNT];
    heap_caps_free(p->buf);   /* overwrite the oldest */
    *p = (preview_item_t){ .buf = buf, .len = fb->len, .ts = ts,
                           .change_x10 = change_x10, .seq = ++s_prev_seq };
    xSemaphoreGive(s_prev_mtx);
    ESP_LOGI(TAG, "Preview stored #%" PRIu32, p->seq);
}

/* GET /img?n=<seq>  or  /latest.jpg */
static esp_err_t img_get(httpd_req_t *req)
{
    char q[32], v[12];
    uint32_t n = 0;
    if (httpd_req_get_url_query_str(req, q, sizeof(q)) == ESP_OK &&
        httpd_query_key_value(q, "n", v, sizeof(v)) == ESP_OK) {
        n = strtoul(v, NULL, 10);
    }
    esp_err_t err;
    xSemaphoreTake(s_prev_mtx, portMAX_DELAY);
    if (n == 0) n = s_prev_seq;   /* latest */
    const preview_item_t *p = n ? &s_prev[(n - 1) % CONFIG_PREVIEW_COUNT] : NULL;
    if (!p || p->seq != n || !p->buf) {
        err = httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "image not stored");
    } else {
        httpd_resp_set_type(req, "image/jpeg");
        httpd_resp_set_hdr(req, "Cache-Control", "no-store");
        err = httpd_resp_send(req, (const char *)p->buf, p->len);
    }
    xSemaphoreGive(s_prev_mtx);
    return err;
}

/* GET /  -> gallery, newest first */
static esp_err_t index_get(httpd_req_t *req)
{
    char line[384];
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    snprintf(line, sizeof(line),
             "<!doctype html><html><head><meta name=viewport content='width=device-width'>"
             "<meta http-equiv=refresh content=%d><title>%s</title>"
             "<style>body{font-family:sans-serif;margin:16px}img{max-width:100%%}</style>"
             "</head><body><h3>%s &mdash; kept images (newest first)</h3>",
             CONFIG_CAPTURE_INTERVAL_SEC < 60 ? CONFIG_CAPTURE_INTERVAL_SEC : 60,
             CONFIG_DEVICE_ID, CONFIG_DEVICE_ID);
    httpd_resp_sendstr_chunk(req, line);

    xSemaphoreTake(s_prev_mtx, portMAX_DELAY);
    uint32_t last = s_prev_seq;
    for (uint32_t k = last; k > 0 && k + CONFIG_PREVIEW_COUNT > last; k--) {
        const preview_item_t *p = &s_prev[(k - 1) % CONFIG_PREVIEW_COUNT];
        if (p->seq != k || !p->buf) continue;

        char when[32] = "time not synced";
        if (p->ts > 1600000000) {
            struct tm tm;
            localtime_r(&p->ts, &tm);
            strftime(when, sizeof(when), "%Y-%m-%d %H:%M:%S", &tm);
        }
        char chg[24] = "first image";
        if (p->change_x10 >= 0) {
            snprintf(chg, sizeof(chg), "change %d.%d%%", p->change_x10 / 10, p->change_x10 % 10);
        }
        snprintf(line, sizeof(line),
                 "<p>#%" PRIu32 " &middot; %s &middot; %s &middot; %u KB<br>"
                 "<img src='/img?n=%" PRIu32 "'></p>",
                 p->seq, when, chg, (unsigned)(p->len / 1024), p->seq);
        httpd_resp_sendstr_chunk(req, line);
    }
    xSemaphoreGive(s_prev_mtx);

    if (last == 0) httpd_resp_sendstr_chunk(req, "<p>No images yet.</p>");
    httpd_resp_sendstr_chunk(req, "</body></html>");
    return httpd_resp_sendstr_chunk(req, NULL);
}

static void preview_init(void)
{
    s_prev_mtx = xSemaphoreCreateMutex();

    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    httpd_handle_t srv;
    if (httpd_start(&srv, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "Preview web server failed to start");
        return;
    }
    const httpd_uri_t uris[] = {
        { .uri = "/",           .method = HTTP_GET, .handler = index_get },
        { .uri = "/img",        .method = HTTP_GET, .handler = img_get },
        { .uri = "/latest.jpg", .method = HTTP_GET, .handler = img_get },
    };
    for (size_t i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
        httpd_register_uri_handler(srv, &uris[i]);
    }
}
#endif

/* ================================================================
 *  Capture loop
 * ================================================================ */
static void capture_task(void *arg)
{
    TickType_t last_wake = xTaskGetTickCount();
    /* not pdMS_TO_TICKS(sec * 1000): that overflows 32 bits above ~71 min at 1 kHz */
    const TickType_t period = (TickType_t)CONFIG_CAPTURE_INTERVAL_SEC * configTICK_RATE_HZ;

    while (1) {
        gpio_set_level(LED_CAPTURE, 1);

        /* drop one possibly stale frame, then take a fresh one */
        camera_fb_t *fb = esp_camera_fb_get();
        if (fb) esp_camera_fb_return(fb);
        fb = esp_camera_fb_get();

        if (!fb) {
            ESP_LOGE(TAG, "Capture failed");
            gpio_set_level(LED_CAPTURE, 0);
            led_blink(LED_SAVED, 5, 60);
            vTaskDelayUntil(&last_wake, period);
            continue;
        }

        time_t now; time(&now);
        int w = 0, h = 0;
        bool ok = jpeg_to_gray_thumb(fb, s_new_gray, &w, &h);
        bool keep;
        int change_x10 = -1;

        if (!ok) {
            keep = false;
        } else if (!s_has_ref || w != s_ref_w || h != s_ref_h) {
            keep = true;     /* first image after boot is always kept */
            ESP_LOGI(TAG, "First image -> keep");
        } else {
            change_x10 = compare_thumbs(s_ref_gray, s_new_gray, w * h);
            keep = change_x10 >= CONFIG_CHANGE_PERCENT_X10;
            ESP_LOGI(TAG, "Change %d.%d %% -> %s", change_x10 / 10, change_x10 % 10,
                     keep ? "KEEP" : "discard");
        }

        if (keep) {
            jpeg_item_t it = { .len = fb->len, .ts = now };
            it.buf = heap_caps_malloc(fb->len, MALLOC_CAP_SPIRAM);
            if (it.buf) {
                memcpy(it.buf, fb->buf, fb->len);
                /* new image becomes the reference */
                memcpy(s_ref_gray, s_new_gray, w * h);
                s_ref_w = w; s_ref_h = h; s_has_ref = true;
                queue_for_upload(&it);
#if CONFIG_PREVIEW_ENABLE
                preview_store(fb, now, change_x10);
#endif
                gpio_set_level(LED_CAPTURE, 0);
                led_blink(LED_SAVED, 2, 120);
            } else {
                ESP_LOGE(TAG, "No PSRAM for image copy");
            }
        }

        esp_camera_fb_return(fb);
        gpio_set_level(LED_CAPTURE, 0);
        ESP_LOGI(TAG, "Free PSRAM: %u KB", (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));

        vTaskDelayUntil(&last_wake, period);
    }
}

/* ================================================================
 *  app_main
 * ================================================================ */
void app_main(void)
{
    leds_init();

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    s_wifi_ev  = xEventGroupCreate();
    s_upload_q = xQueueCreate(CONFIG_UPLOAD_QUEUE_LEN, sizeof(jpeg_item_t));

    s_rgb565   = heap_caps_malloc(THUMB_MAX_PIXELS * 2, MALLOC_CAP_SPIRAM);
    s_ref_gray = heap_caps_malloc(THUMB_MAX_PIXELS, MALLOC_CAP_SPIRAM);
    s_new_gray = heap_caps_malloc(THUMB_MAX_PIXELS, MALLOC_CAP_SPIRAM);
    if (!s_rgb565 || !s_ref_gray || !s_new_gray) {
        fatal_blink_restart("PSRAM allocation failed (is PSRAM enabled?)");
    }

    if (camera_init() != ESP_OK) {
        fatal_blink_restart("camera init");
    }

    wifi_init();
#if CONFIG_CAM_WIFI_STA
    time_init();   /* no internet in AP mode */
#endif
#if CONFIG_PREVIEW_ENABLE
    preview_init();
#endif

    xTaskCreate(status_led_task, "status_led", 2048, NULL, 2, NULL);
    xTaskCreate(upload_task,     "upload",     8192, NULL, 4, NULL);
    xTaskCreate(capture_task,    "capture",    8192, NULL, 5, NULL);
}
