/* Minimal LED blink test firmware for STM32F4 Discovery (Zephyr).
 *
 * Toggles the user LED (LD1, PC13) every 500 ms. No console, no UART.
 * If the LED blinks, the flash + toolchain + board bring-up is working.
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>

static const struct gpio_dt_spec led =
	GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);

int main(void)
{
	if (gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE) != 0) {
		return 0;
	}

	while (1) {
		gpio_pin_set_dt(&led, 1);
		k_msleep(500);
		gpio_pin_set_dt(&led, 0);
		k_msleep(500);
	}

	return 0;
}
