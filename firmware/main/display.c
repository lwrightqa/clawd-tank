// firmware/main/display.c
//
// MiniBar bar port: the Waveshare ESP32-S3-Touch-LCD-3.49 V2's AXS15231B panel (172 x 640 native, used as 640 x 172)
// over QSPI, for LVGL 9. The bring-up is taken from the MiniBar project's board layer
// (components/board/src/board_display.c), itself a port of Waveshare's 10_LVGL_V9_Test, kept to its proven settings:
//   - the power hold and the panel reset go through the TCA9554 expander (board_exio.c), the reset on EXIO5 as
//     high 30 ms, low 250 ms, high 30 ms, then init commands 0x11 (sleep out) and 0x29 (display on), 100 ms each;
//   - QSPI on SPI3 at 40 MHz, mode 3, 32-bit commands, CS on GPIO 9, no DC line;
//   - the LVGL display at the native 172 x 640 with one full-frame RGB565 buffer in PSRAM
//     (LV_DISPLAY_RENDER_MODE_FULL) and software rotation: the flush callback turns the frame with
//     lv_draw_sw_rotate() into a second PSRAM frame (rotation 270 is upright, side buttons on top, on the V2 board;
//     CONFIG_CLAWD_LCD_TURN_180 makes it 90), then sends it in ten 64-line chunks through one internal DMA buffer,
//     each chunk waiting for the previous transfer. In QSPI mode the AXS15231B takes no row address: a chunk at
//     row 0 is a RAMWR (0x2C) and the others continue it (RAMWRC, 0x3C), so a frame is always sent whole and in
//     order. The RGB565 byte swap happens in the DMA buffer, so LVGL's buffer is never modified;
//   - the backlight: LEDC PWM on GPIO 42 into the AP3032's feedback pin (high = dimmer, dark from duty 164 of 255)
//     plus BL_EN on EXIO1. Nothing lights before the first frame has reached the panel. The 0..255 brightness the
//     rest of the firmware uses (config_store, BLE) goes through MiniBar's brightness curve (brd_bl_duty).
// Every wait has a time-out, so a dead panel can't hang ui_task; if the panel fails to init, the display is still
// created (it draws nowhere, and the log says why) so BLE keeps working.
#include "display.h"
#include "board_exio.h"
#include "board_pins.h"
#include "config_store.h"

#include <inttypes.h>
#include <math.h>
#include <string.h>

#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_lcd_axs15231b.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "display";

#define LCD_PCLK_HZ       (40 * 1000 * 1000)                         /* as Waveshare's 10_LVGL_V9_Test */
#define LCD_CHUNK_LINES   64
#define LCD_CHUNK_PX      (BOARD_LCD_H_RES * LCD_CHUNK_LINES)
#define LCD_CHUNK_BYTES   (LCD_CHUNK_PX * 2)                         /* 22016 */
#define LCD_CHUNKS        (BOARD_LCD_V_RES / LCD_CHUNK_LINES)        /* 10 */
#define LCD_FRAME_BYTES   (BOARD_LCD_H_RES * BOARD_LCD_V_RES * 2)    /* 220160 */
#define LCD_WAIT_MS       100                                        /* one chunk takes about 1.1 ms */

#define BL_LEDC_TIMER     LEDC_TIMER_3
#define BL_LEDC_CHANNEL   LEDC_CHANNEL_1
#define BL_LEDC_HZ        50000
#define BL_DUTY_MAX       255   /* pin always high: no LED current at all */
#define BL_ZERO_DUTY      164   /* duty (of 255) where the LED current reaches zero, from the V2 schematic (MiniBar) */

#if CONFIG_CLAWD_LCD_TURN_180
#define LCD_ROTATION      LV_DISPLAY_ROTATION_90
#define LCD_ROTATION_DEG  90
#else
#define LCD_ROTATION      LV_DISPLAY_ROTATION_270
#define LCD_ROTATION_DEG  270
#endif

_Static_assert(BOARD_LCD_V_RES % LCD_CHUNK_LINES == 0, "chunks must cover the panel exactly");

static const axs15231b_lcd_init_cmd_t s_init_cmds[] = {
    {0x11, (uint8_t[]){0x00}, 0, 100},
    {0x29, (uint8_t[]){0x00}, 0, 100},
};

static lv_display_t *s_disp;
static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_trans_done;
static uint16_t *s_dma;             /* one chunk, internal DMA-capable RAM */
static uint8_t *s_rot;              /* the rotated frame, PSRAM */
static bool s_panel_ok;
static uint32_t s_flush_errors;

/* Backlight state. */
static bool s_ledc_ok;
static bool s_frame_shown;          /* the first frame has reached the panel */
static uint8_t s_brightness;        /* 0..255 as the rest of the firmware sees it; 0 = off */
static int s_bl_duty_now = -1;

/* ---------- Backlight ---------- */

/* MiniBar's brightness curve (brd_bl_level / brd_bl_duty in components/board/logic/brd_inputs.c): the level as a
 * fraction p gives a brightness factor b = 0.45 + 0.55 p, shown as luminance b^2.2, which is the LED current
 * fraction L; the pin's high duty is then zero_duty * (1 - L). 0 is fully dark: pin high and the driver disabled. */
static uint8_t bl_duty_for(uint8_t brightness)
{
    if (brightness == 0) return BL_DUTY_MAX;
    float b = 0.45f + 0.55f * (float)brightness / 255.0f;
    float level = powf(b, 2.2f);
    long d = lroundf((float)BL_ZERO_DUTY * (1.0f - level));
    if (d < 0) d = 0;
    if (d > BL_DUTY_MAX) d = BL_DUTY_MAX;
    return (uint8_t)d;
}

static void bl_write(uint8_t brightness)
{
    uint8_t duty = bl_duty_for(brightness);
    if (s_ledc_ok && duty != s_bl_duty_now) {
        ledc_set_duty(LEDC_LOW_SPEED_MODE, BL_LEDC_CHANNEL, duty);
        ledc_update_duty(LEDC_LOW_SPEED_MODE, BL_LEDC_CHANNEL);
        s_bl_duty_now = duty;
    }
    board_exio_set(BOARD_EXIO_BL_EN, brightness > 0);
}

static void bl_init_pwm(void)
{
    ledc_timer_config_t t = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_8_BIT,
        .timer_num = BL_LEDC_TIMER,
        .freq_hz = BL_LEDC_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ledc_channel_config_t c = {
        .gpio_num = BOARD_LCD_BL,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = BL_LEDC_CHANNEL,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = BL_LEDC_TIMER,
        .duty = BL_DUTY_MAX,        /* dark until the first frame */
        .hpoint = 0,
    };
    s_ledc_ok = ledc_timer_config(&t) == ESP_OK && ledc_channel_config(&c) == ESP_OK;
    s_bl_duty_now = BL_DUTY_MAX;
    if (!s_ledc_ok) ESP_LOGE(TAG, "backlight PWM setup failed; BL_EN alone will switch it");
}

void display_set_brightness(uint8_t duty)
{
    s_brightness = duty;
    if (!s_frame_shown) return;     /* the first flush applies it */
    bl_write(duty);
    ESP_LOGI(TAG, "Brightness set to %u", duty);
}

/* ---------- Panel ---------- */

static bool IRAM_ATTR on_trans_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *edata, void *ctx)
{
    (void)io;
    (void)edata;
    (void)ctx;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_trans_done, &woken);
    return woken == pdTRUE;
}

static void lcd_reset(void)
{
    board_exio_set(BOARD_EXIO_LCD_RST, true);
    vTaskDelay(pdMS_TO_TICKS(30));
    board_exio_set(BOARD_EXIO_LCD_RST, false);
    vTaskDelay(pdMS_TO_TICKS(250));
    board_exio_set(BOARD_EXIO_LCD_RST, true);
    vTaskDelay(pdMS_TO_TICKS(30));
}

static esp_err_t panel_init(void)
{
    spi_bus_config_t bus = {
        .sclk_io_num = BOARD_LCD_PCLK,
        .data0_io_num = BOARD_LCD_D0,
        .data1_io_num = BOARD_LCD_D1,
        .data2_io_num = BOARD_LCD_D2,
        .data3_io_num = BOARD_LCD_D3,
        .data4_io_num = -1,
        .data5_io_num = -1,
        .data6_io_num = -1,
        .data7_io_num = -1,
        .max_transfer_sz = LCD_CHUNK_BYTES,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize((spi_host_device_t)BOARD_LCD_HOST, &bus, SPI_DMA_CH_AUTO), TAG, "QSPI bus");

    esp_lcd_panel_io_spi_config_t io = {
        .cs_gpio_num = BOARD_LCD_CS,
        .dc_gpio_num = -1,
        .spi_mode = 3,
        .pclk_hz = LCD_PCLK_HZ,
        .trans_queue_depth = 10,
        .on_color_trans_done = on_trans_done,
        .lcd_cmd_bits = 32,
        .lcd_param_bits = 8,
        .flags.quad_mode = true,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BOARD_LCD_HOST, &io, &s_io), TAG,
                        "panel IO");

    axs15231b_vendor_config_t vendor = {
        .init_cmds = s_init_cmds,
        .init_cmds_size = sizeof s_init_cmds / sizeof s_init_cmds[0],
        .flags.use_qspi_interface = 1,
    };
    esp_lcd_panel_dev_config_t dev = {
        .reset_gpio_num = -1,       /* the reset line is on the expander (lcd_reset) */
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = &vendor,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_axs15231b(s_io, &dev, &s_panel), TAG, "AXS15231B");
    lcd_reset();
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    return ESP_OK;
}

/* Send one native 172 x 640 RGB565 frame (LVGL's byte order) to the panel. */
static bool send_frame(const uint8_t *frame)
{
    xSemaphoreTake(s_trans_done, 0);    /* drop a stale completion */
    for (int i = 0; i < LCD_CHUNKS; i++) {
        if (i > 0 && xSemaphoreTake(s_trans_done, pdMS_TO_TICKS(LCD_WAIT_MS)) != pdTRUE) return false;
        memcpy(s_dma, frame + (size_t)i * LCD_CHUNK_BYTES, LCD_CHUNK_BYTES);
        lv_draw_sw_rgb565_swap(s_dma, LCD_CHUNK_PX);    /* the panel wants big-endian pixels */
        if (esp_lcd_panel_draw_bitmap(s_panel, 0, i * LCD_CHUNK_LINES, BOARD_LCD_H_RES, (i + 1) * LCD_CHUNK_LINES,
                                      s_dma) != ESP_OK) {
            return false;
        }
    }
    return xSemaphoreTake(s_trans_done, pdMS_TO_TICKS(LCD_WAIT_MS)) == pdTRUE;
}

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px)
{
    const uint8_t *frame = px;
    lv_display_rotation_t rot = lv_display_get_rotation(disp);
    if (rot != LV_DISPLAY_ROTATION_0) {
        lv_area_t turned = *area;
        lv_display_rotate_area(disp, &turned);
        int32_t w = lv_area_get_width(area), h = lv_area_get_height(area);
        uint32_t src_stride = lv_draw_buf_width_to_stride(w, LV_COLOR_FORMAT_RGB565);
        uint32_t dst_stride = lv_draw_buf_width_to_stride(lv_area_get_width(&turned), LV_COLOR_FORMAT_RGB565);
        lv_draw_sw_rotate(px, s_rot, w, h, src_stride, dst_stride, rot, LV_COLOR_FORMAT_RGB565);
        frame = s_rot;
    }
    if (s_panel_ok && send_frame(frame)) {
        if (!s_frame_shown) {
            s_frame_shown = true;
            bl_write(s_brightness);
            ESP_LOGI(TAG, "first frame shown, backlight %u/255", s_brightness);
        }
    } else if (s_panel_ok && (s_flush_errors++ % 100) == 0) {
        ESP_LOGE(TAG, "panel transfer timed out (%lu so far)", (unsigned long)s_flush_errors);
    }
    lv_display_flush_ready(disp);
}

static uint32_t tick_cb(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

lv_display_t *display_init(void)
{
    ESP_LOGI(TAG, "Initializing display (AXS15231B over QSPI, %dx%d)...", BOARD_SCREEN_W, BOARD_SCREEN_H);
    s_brightness = config_store_get_brightness();

    /* The power hold and the expander first (idempotent: main calls it at the top of app_main). Without the expander
     * there is no panel reset and no backlight enable. */
    esp_err_t err = board_power_hold();
    if (err != ESP_OK) ESP_LOGE(TAG, "power hold / TCA9554 failed (%s)", esp_err_to_name(err));

    bl_init_pwm();
    board_exio_set(BOARD_EXIO_BL_EN, false);

    s_trans_done = xSemaphoreCreateBinary();
    s_dma = heap_caps_malloc(LCD_CHUNK_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    uint8_t *b1 = heap_caps_aligned_alloc(64, LCD_FRAME_BYTES, MALLOC_CAP_SPIRAM);
    s_rot = heap_caps_aligned_alloc(64, LCD_FRAME_BYTES, MALLOC_CAP_SPIRAM);
    if (!s_trans_done || !s_dma || !b1 || !s_rot) {
        ESP_LOGE(TAG, "no memory for the frame buffers (2 x %d PSRAM + %d internal)", LCD_FRAME_BYTES, LCD_CHUNK_BYTES);
        abort();
    }

    err = panel_init();
    s_panel_ok = err == ESP_OK;
    if (!s_panel_ok) {
        ESP_LOGE(TAG, "panel init failed (%s): running without a screen", esp_err_to_name(err));
    }

    /* LVGL init; the tick reads esp_timer (no tick timer needed). */
    lv_init();
    lv_tick_set_cb(tick_cb);

    s_disp = lv_display_create(BOARD_LCD_H_RES, BOARD_LCD_V_RES);
    if (!s_disp) {
        ESP_LOGE(TAG, "lv_display_create failed — out of memory");
        abort();
    }
    lv_display_set_color_format(s_disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(s_disp, b1, NULL, LCD_FRAME_BYTES, LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_flush_cb(s_disp, flush_cb);
    lv_display_set_rotation(s_disp, LCD_ROTATION);

    ESP_LOGI(TAG, "Display initialized: %" PRId32 "x%" PRId32 " landscape (native %dx%d, rotation %d)",
             lv_display_get_horizontal_resolution(s_disp), lv_display_get_vertical_resolution(s_disp),
             BOARD_LCD_H_RES, BOARD_LCD_V_RES, LCD_ROTATION_DEG);
    return s_disp;
}
