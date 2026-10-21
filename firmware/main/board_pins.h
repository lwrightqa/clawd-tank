/*
 * firmware/main/board_pins.h: the Waveshare ESP32-S3-Touch-LCD-3.49 V2 (the MiniBar bar) pin map, the part this
 * port uses. From the MiniBar project (components/board/include/board_pins.h), which took it from Waveshare's
 * ESP-IDF examples (11_FactoryProgram/main/user_config.h) and the V2 schematic.
 */
#pragma once

/* System I2C bus (I2C_NUM_0): the TCA9554 expander (and the PCF85063 RTC, QMI8658 IMU, ES8311 codec, unused here). */
#define BOARD_I2C_SYS_PORT      0
#define BOARD_I2C_SYS_SDA       47
#define BOARD_I2C_SYS_SCL       48

/* LCD: AXS15231B over QSPI on SPI3, 172 x 640 native (portrait), used as 640 x 172. */
#define BOARD_LCD_HOST          2       /* SPI3_HOST (spi_host_device_t: SPI1 = 0, SPI2 = 1, SPI3 = 2) */
#define BOARD_LCD_CS            9
#define BOARD_LCD_PCLK          10
#define BOARD_LCD_D0            11
#define BOARD_LCD_D1            12
#define BOARD_LCD_D2            13
#define BOARD_LCD_D3            14
#define BOARD_LCD_TE            21      /* tearing-effect output; unused (as in Waveshare's examples) */
#define BOARD_LCD_BL            42      /* PWM into the AP3032's feedback: high = dimmer; dark from about 64% high */
#define BOARD_LCD_H_RES         172     /* native */
#define BOARD_LCD_V_RES         640
#define BOARD_SCREEN_W          640     /* logical, landscape */
#define BOARD_SCREEN_H          172

/* TCA9554 I/O expander pins (bit masks); the chip is at I2C address 0x20 (A0..A2 grounded). */
#define BOARD_EXIO_TOUCH_INT    (1u << 0)
#define BOARD_EXIO_BL_EN        (1u << 1)   /* AP3032 CTRL; 100k pull-up on the board, so ON until driven low */
#define BOARD_EXIO_IMU_INT1     (1u << 2)
#define BOARD_EXIO_IMU_INT2     (1u << 3)
#define BOARD_EXIO_RTC_INT      (1u << 4)
#define BOARD_EXIO_LCD_RST      (1u << 5)
#define BOARD_EXIO_SYS_EN       (1u << 6)   /* power hold on battery: set high FIRST at boot */
#define BOARD_EXIO_NS_MODE      (1u << 7)   /* NS4150B amplifier CTRL: high = on; 10k pull-down, so off by default */
#define BOARD_EXIO_INT_GPIO     8           /* the expander's interrupt line */

/* Buttons: plain GPIOs (not behind the expander), active low, 10k pull-ups on the board. */
#define BOARD_BTN_BOOT          0
#define BOARD_BTN_PWR           16          /* SYS_OUT: the PWR key; also the deep-sleep wake (unused here) */
