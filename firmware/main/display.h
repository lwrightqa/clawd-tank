// firmware/main/display.h
#ifndef DISPLAY_H
#define DISPLAY_H

#include "lvgl.h"

// MiniBar bar (Waveshare ESP32-S3-Touch-LCD-3.49 V2): hold the power, bring up the AXS15231B panel over QSPI,
// create the LVGL display (640 x 172 landscape, software-rotated from the native 172 x 640) and set up the
// backlight PWM. Calls lv_init(). Returns the LVGL display object. The backlight comes on with the first frame.
lv_display_t *display_init(void);

// Set backlight brightness, 0-255 (0 = off). Mapped onto the bar's inverted backlight PWM (GPIO 42) and its
// enable line on the expander; before the first frame it only records the level.
void display_set_brightness(uint8_t duty);

// Switch the screen off (the backlight goes dark and stays dark whatever brightness is set) or back on at the
// last level set. The PWR key toggles this.
void display_set_screen_off(bool off);

#endif // DISPLAY_H
