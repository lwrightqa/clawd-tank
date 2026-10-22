#pragma once
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

// Initialize the top keys: GPIO 0 (BOOT) posts BLE_EVT_NOTIF_CLEAR on press, GPIO 16 (PWR) posts BLE_EVT_SCREEN_TOGGLE.
void button_init(QueueHandle_t evt_queue);
