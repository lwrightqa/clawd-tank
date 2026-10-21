// firmware/main/board_exio.h
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

/* MiniBar bar (Waveshare ESP32-S3-Touch-LCD-3.49 V2): the TCA9554 I/O expander behind the battery power hold, the
 * LCD reset and the backlight enable. From the MiniBar project's board layer; see board_exio.c. */

/* The power-on sequence, to run first in app_main: LCD_BL (GPIO 42) to its dark level, the system I2C bus, then the
 * expander with SYS_EN high (power held on battery), LCD_RST high and BL_EN low. Idempotent. */
esp_err_t board_power_hold(void);

/* Drive expander output pins (BOARD_EXIO_* masks from board_pins.h). Any task; writes only on change. */
esp_err_t board_exio_set(uint8_t mask, bool high);
bool board_exio_ready(void);
esp_err_t board_exio_read_inputs(uint8_t *levels);
