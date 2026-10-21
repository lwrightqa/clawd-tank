#pragma once
#include <stdbool.h>
#include "lvgl.h"

/* Which layout the display gets. One build of scene.c, notification_ui.c and ui_manager.c serves two
 * displays, chosen at run time from the LVGL display's width (after rotation):
 *   320 x 172 (the Waveshare ESP32-C6-LCD-1.47 and the simulator) keeps the original layout unchanged;
 *   anything wider (the MiniBar bar, 640 x 172) gets the wide layout described in docs/bar/.
 * Nothing here is a compile-time switch, so the simulator and the device compile the same code. */
#define LAYOUT_BASE_WIDTH 320

static inline int layout_display_width(void)
{
    return lv_display_get_horizontal_resolution(NULL);
}

static inline bool layout_is_wide(int display_w)
{
    return display_w > LAYOUT_BASE_WIDTH;
}
