# firmware/main/CMakeLists.txt
idf_component_register(
    SRCS "main.c" "display.c" "board_exio.c" "board_lv_mem.c" "ble_service.c" "notification.c" "ui_manager.c" "scene.c" "notification_ui.c" "config_store.c" "rgb_led.c" "pixel_font.c" "button.c"
    INCLUDE_DIRS "."
    REQUIRES esp_lcd driver esp_driver_gpio esp_driver_i2c esp_driver_ledc esp_driver_spi esp_timer bt nvs_flash freertos json
)

# LVGL calls the memory functions board_lv_mem.c defines (CONFIG_LV_USE_CUSTOM_MALLOC), but main is linked before
# lvgl, so pull that object in up front (as MiniBar's components/board/CMakeLists.txt does).
target_link_libraries(${COMPONENT_LIB} INTERFACE "-u lv_malloc_core" "-u lv_realloc_core" "-u lv_free_core"
                      "-u lv_mem_init")
