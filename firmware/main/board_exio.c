// firmware/main/board_exio.c
/*
 * The TCA9554 I/O expander and the power hold of the Waveshare ESP32-S3-Touch-LCD-3.49 V2 (the MiniBar bar).
 * Taken from the MiniBar project's board layer (components/board/src/board_exio.c and board_power.c), which follows
 * Waveshare's 11_FactoryProgram (tca9554_init, example_lcd_exio_init) and 07_BATT_PWR_Test.
 *
 * Registers: 0 input, 1 output, 2 polarity inversion, 3 configuration (1 = input). This is not
 * esp_io_expander_tca9554 because that driver's constructor resets every pin to an input, which on a software
 * restart would let go of SYS_EN (the battery power hold) and of BL_EN's low for a moment. This one writes the output
 * register first and only then the direction register, so no pin glitches.
 *
 * Power (the V2 schematic): on battery the system rail comes through a P-MOSFET held on by the PWR key or by SYS_EN
 * (EXIO6), so the firmware must drive SYS_EN high before the key is released; on USB SYS_EN changes nothing.
 */
#include "board_exio.h"
#include "board_pins.h"

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "board.exio";

#define BOARD_I2C_HZ          300000  /* Waveshare's examples run the bus at 300 kHz */
#define BOARD_I2C_TIMEOUT_MS  50
#define BOARD_ADDR_EXIO       0x20

#define REG_INPUT    0x00
#define REG_OUTPUT   0x01
#define REG_POLARITY 0x02
#define REG_CONFIG   0x03

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static SemaphoreHandle_t s_mux;
static uint8_t s_out = 0xff;

static esp_err_t write_reg(uint8_t reg, uint8_t val)
{
    uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s_dev, b, sizeof b, BOARD_I2C_TIMEOUT_MS);
}

static esp_err_t exio_init(i2c_master_bus_handle_t bus, uint8_t outputs_mask, uint8_t initial_levels)
{
    if (s_dev) return ESP_OK;
    if (!s_mux) s_mux = xSemaphoreCreateMutex();
    ESP_RETURN_ON_FALSE(s_mux, ESP_ERR_NO_MEM, TAG, "mutex");
    i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = BOARD_ADDR_EXIO,
        .scl_speed_hz = BOARD_I2C_HZ,
    };
    ESP_RETURN_ON_ERROR(i2c_master_bus_add_device(bus, &cfg, &s_dev), TAG, "add TCA9554");
    /* Output levels first (pins that are still inputs take them the moment they become outputs), then polarity, then
     * the direction. Inputs keep a 1 in the output register, the chip's reset value. */
    uint8_t out = (uint8_t)((initial_levels & outputs_mask) | (uint8_t)~outputs_mask);
    esp_err_t err = ESP_FAIL;
    for (int attempt = 0; attempt < 3 && err != ESP_OK; attempt++) {
        err = write_reg(REG_OUTPUT, out);
        if (err == ESP_OK) err = write_reg(REG_POLARITY, 0x00);
        if (err == ESP_OK) err = write_reg(REG_CONFIG, (uint8_t)~outputs_mask);
        if (err != ESP_OK) vTaskDelay(pdMS_TO_TICKS(2));
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "TCA9554 not answering (%s)", esp_err_to_name(err));
        i2c_master_bus_rm_device(s_dev);
        s_dev = NULL;
        return err;
    }
    s_out = out;
    return ESP_OK;
}

esp_err_t board_power_hold(void)
{
    if (s_bus && s_dev) return ESP_OK;
    /* LCD_BL to its dark level (high) before anything else: BL_EN has a 100k pull-up, so until the expander drives it
     * low the backlight driver is enabled. The level is set before the pin becomes an output so it never dips. */
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BOARD_LCD_BL,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_set_level(BOARD_LCD_BL, 1);
    gpio_config(&io);
    gpio_set_level(BOARD_LCD_BL, 1);

    if (!s_bus) {
        i2c_master_bus_config_t cfg = {
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .i2c_port = BOARD_I2C_SYS_PORT,
            .scl_io_num = BOARD_I2C_SYS_SCL,
            .sda_io_num = BOARD_I2C_SYS_SDA,
            .glitch_ignore_cnt = 7,
            .flags.enable_internal_pullup = true,
        };
        ESP_RETURN_ON_ERROR(i2c_new_master_bus(&cfg, &s_bus), TAG, "system I2C bus");
    }
    /* SYS_EN high (power held), LCD reset high (not in reset), BL_EN low (dark), the amplifier off. */
    uint8_t outputs = BOARD_EXIO_SYS_EN | BOARD_EXIO_NS_MODE | BOARD_EXIO_BL_EN | BOARD_EXIO_LCD_RST;
    uint8_t levels = BOARD_EXIO_SYS_EN | BOARD_EXIO_LCD_RST;
    ESP_RETURN_ON_ERROR(exio_init(s_bus, outputs, levels), TAG, "TCA9554");
    ESP_LOGI(TAG, "power held (SYS_EN high)");
    return ESP_OK;
}

bool board_exio_ready(void)
{
    return s_dev != NULL;
}

esp_err_t board_exio_set(uint8_t mask, bool high)
{
    if (!s_dev) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mux, portMAX_DELAY);
    uint8_t out = high ? (uint8_t)(s_out | mask) : (uint8_t)(s_out & ~mask);
    esp_err_t err = ESP_OK;
    if (out != s_out) {
        err = write_reg(REG_OUTPUT, out);
        if (err == ESP_OK) s_out = out;
    }
    xSemaphoreGive(s_mux);
    if (err != ESP_OK) ESP_LOGW(TAG, "set 0x%02x=%d: %s", mask, high, esp_err_to_name(err));
    return err;
}

esp_err_t board_exio_read_inputs(uint8_t *levels)
{
    if (!s_dev) return ESP_ERR_INVALID_STATE;
    uint8_t reg = REG_INPUT;
    xSemaphoreTake(s_mux, portMAX_DELAY);
    esp_err_t err = i2c_master_transmit_receive(s_dev, &reg, 1, levels, 1, BOARD_I2C_TIMEOUT_MS);
    xSemaphoreGive(s_mux);
    return err;
}
