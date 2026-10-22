// firmware/main/button.c
#include "button.h"
#include "ble_service.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "board_pins.h"

/* The ESP32-S3-Touch-LCD-3.49 V2's two top keys, both plain GPIOs, active low with 10k pull-ups:
 *   BOOT (GPIO 0)  dismisses the notifications.
 *   PWR  (GPIO 16) switches the screen off, and on again. It is also the board's power key, but the firmware holds
 *                  the power (main.c) and only the falling edge of a new press counts, so the press that switched
 *                  the bar on cannot also switch the screen off. The RESET key is wired to the chip's reset line.
 * Each key has its own 200 ms debounce. */
#define BUTTON_GPIO     BOARD_BTN_BOOT
#define PWR_GPIO        BOARD_BTN_PWR
#define DEBOUNCE_US     200000  /* 200ms debounce */

static const char *TAG = "button";
static QueueHandle_t s_queue;
static int64_t s_last_boot_us;
static int64_t s_last_pwr_us;

static void IRAM_ATTR button_isr(void *arg)
{
    const int gpio = (int)(intptr_t)arg;
    int64_t *last = (gpio == PWR_GPIO) ? &s_last_pwr_us : &s_last_boot_us;
    int64_t now = esp_timer_get_time();
    if (now - *last < DEBOUNCE_US) return;
    *last = now;

    ble_evt_t evt = { .type = (gpio == PWR_GPIO) ? BLE_EVT_SCREEN_TOGGLE : BLE_EVT_NOTIF_CLEAR };
    BaseType_t woken = pdFALSE;
    xQueueSendFromISR(s_queue, &evt, &woken);
    if (woken) portYIELD_FROM_ISR();
}

void button_init(QueueHandle_t evt_queue)
{
    s_queue = evt_queue;
    s_last_boot_us = 0;
    s_last_pwr_us = 0;

    gpio_config_t cfg = {
        .intr_type    = GPIO_INTR_NEGEDGE,
        .mode         = GPIO_MODE_INPUT,
        .pin_bit_mask = (1ULL << BUTTON_GPIO) | (1ULL << PWR_GPIO),
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
    };
    gpio_config(&cfg);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(BUTTON_GPIO, button_isr, (void *)(intptr_t)BUTTON_GPIO);
    gpio_isr_handler_add(PWR_GPIO, button_isr, (void *)(intptr_t)PWR_GPIO);

    ESP_LOGI(TAG, "GPIO %d BOOT (clear notifications) and GPIO %d PWR (screen off/on) initialized", BUTTON_GPIO, PWR_GPIO);
}
