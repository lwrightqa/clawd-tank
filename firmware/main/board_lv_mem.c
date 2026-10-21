// firmware/main/board_lv_mem.c
/*
 * LVGL's memory functions (CONFIG_LV_USE_CUSTOM_MALLOC) in PSRAM, from the MiniBar project
 * (components/board/src/board_lv_mem.c).
 *
 * With LVGL on plain malloc(), every object, style, label text and small layer stays in internal RAM
 * (CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL keeps allocations under 4 KB there), where NimBLE and the task stacks need
 * the room. LVGL runs only on ui_task and never while the flash cache is off, so PSRAM is safe for all of it. If
 * PSRAM is ever full, internal RAM is the fallback.
 */
#include <stdlib.h>

#include "esp_heap_caps.h"
#include "lvgl.h"

#if LV_USE_STDLIB_MALLOC == LV_STDLIB_CUSTOM

#define LV_CAPS (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)

void lv_mem_init(void)
{
}

void lv_mem_deinit(void)
{
}

lv_mem_pool_t lv_mem_add_pool(void *mem, size_t bytes)
{
    LV_UNUSED(mem);
    LV_UNUSED(bytes);
    return NULL;
}

void lv_mem_remove_pool(lv_mem_pool_t pool)
{
    LV_UNUSED(pool);
}

void *lv_malloc_core(size_t size)
{
    void *p = heap_caps_malloc(size, LV_CAPS);
    return p ? p : malloc(size);
}

void *lv_realloc_core(void *p, size_t new_size)
{
    if (!p) return lv_malloc_core(new_size);
    /* heap_caps_realloc keeps the block in memory with these caps; one that started internal (the fallback) moves. */
    void *q = heap_caps_realloc(p, new_size, LV_CAPS);
    return q ? q : realloc(p, new_size);
}

void lv_free_core(void *p)
{
    heap_caps_free(p);
}

void lv_mem_monitor_core(lv_mem_monitor_t *mon_p)
{
    LV_UNUSED(mon_p);
}

lv_result_t lv_mem_test_core(void)
{
    return LV_RESULT_OK;
}

#endif
