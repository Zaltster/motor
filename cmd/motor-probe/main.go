package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
)

func main() {
	fmt.Println("motor-probe: non-moving hardware access check")
	hostname, _ := os.Hostname()
	fmt.Printf("host=%s os=%s arch=%s uid=%d gid=%d\n", hostname, runtime.GOOS, runtime.GOARCH, os.Getuid(), os.Getgid())

	gpioDevices := listPaths("gpio character devices", "/dev/gpiochip*")
	spiDevices := listPaths("spi devices", "/dev/spidev*")
	i2cDevices := listPaths("i2c devices", "/dev/i2c-*")
	pwmChips := listPaths("pwm sysfs chips", "/sys/class/pwm/pwmchip*")
	sawRP1 := summarizeGPIOChips()

	gpioWritable := anyWritable(gpioDevices)
	spiWritable := anyWritable(spiDevices)
	pwmWritable := false
	for _, chip := range pwmChips {
		if canWrite(filepath.Join(chip, "export")) {
			pwmWritable = true
			break
		}
	}

	fmt.Println("verdict:")
	switch {
	case sawRP1 && gpioWritable:
		fmt.Println("  YES: this app can see writable Raspberry Pi GPIO hardware.")
		fmt.Println("  For the L298N rig, use GPIO12=ENA, GPIO17=IN1, GPIO18=IN2.")
	case sawRP1:
		fmt.Println("  PARTIAL: Raspberry Pi GPIO hardware is visible, but no writable gpiochip was exposed.")
	default:
		fmt.Println("  NO: Raspberry Pi GPIO hardware was not visible inside this app.")
	}

	if spiWritable {
		fmt.Println("  SPI is writable if the motor controller is SPI-based.")
	}
	if pwmWritable {
		fmt.Println("  Kernel PWM export is writable.")
	}
	if len(i2cDevices) == 0 {
		fmt.Println("  No I2C controller device was visible in this entitlement set.")
	}
	fmt.Println("  No motor command was sent by this probe.")
}

func listPaths(label string, pattern string) []string {
	paths, _ := filepath.Glob(pattern)
	fmt.Printf("%s: %d\n", label, len(paths))
	for _, path := range paths {
		info, err := os.Stat(path)
		mode := "unavailable"
		if err == nil {
			mode = info.Mode().String()
		}
		fmt.Printf("  %s  %s  %s\n", path, mode, accessText(path))
	}
	return paths
}

func summarizeGPIOChips() bool {
	chips, _ := filepath.Glob("/sys/class/gpio/gpiochip*")
	fmt.Printf("sysfs gpio chips: %d\n", len(chips))
	sawRP1 := false
	for _, chip := range chips {
		label := readText(filepath.Join(chip, "label"), "unknown")
		base := readText(filepath.Join(chip, "base"), "unknown")
		ngpio := readText(filepath.Join(chip, "ngpio"), "unknown")
		if strings.Contains(label, "rp1") {
			sawRP1 = true
		}
		fmt.Printf("  %s label=%s base=%s ngpio=%s\n", chip, label, base, ngpio)
	}
	return sawRP1
}

func readText(path string, fallback string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return fallback
	}
	return strings.TrimSpace(string(data))
}

func anyWritable(paths []string) bool {
	for _, path := range paths {
		if canWrite(path) {
			return true
		}
	}
	return false
}

func accessText(path string) string {
	parts := make([]string, 0, 2)
	if canRead(path) {
		parts = append(parts, "read")
	}
	if canWrite(path) {
		parts = append(parts, "write")
	}
	if len(parts) == 0 {
		return "no access"
	}
	return strings.Join(parts, ",")
}

func canRead(path string) bool {
	return syscall.Access(path, 4) == nil
}

func canWrite(path string) bool {
	return syscall.Access(path, 2) == nil
}
