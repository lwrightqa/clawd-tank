// firmware/main/display.h
#ifndef DISPLAY_H
#define DISPLAY_H

#include "lvgl.h"

// Initialize SPI bus, ST7789 panel, LVGL display, and tick timer.
// Returns the LVGL display object. Starts backlight.
lv_display_t *display_init(void);

// Set backlight brightness (0-255 PWM duty cycle).
void display_set_brightness(uint8_t duty);

#endif // DISPLAY_H
