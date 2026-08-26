//go:build windows

package main

import (
	"errors"
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

const agentMSIUpgradeCode = `{A605EA5D-025B-44C5-8823-3AA58EB66A21}`

var (
	msiDLL                      = windows.NewLazySystemDLL("msi.dll")
	msiEnumRelatedProductsW     = msiDLL.NewProc("MsiEnumRelatedProductsW")
	productCodePattern          = regexp.MustCompile(`(?i)^\{[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\}$`)
	enumerateRelatedMSIProducts = relatedMSIProductCodes
	uninstallLegacyMSIProduct   = runLegacyMSIUninstall
)

func relatedMSIProductCodes() ([]string, error) {
	if err := msiEnumRelatedProductsW.Find(); err != nil {
		return nil, fmt.Errorf("load Windows Installer product enumeration: %w", err)
	}
	upgradeCode, err := windows.UTF16PtrFromString(agentMSIUpgradeCode)
	if err != nil {
		return nil, err
	}
	var products []string
	for index := uint32(0); ; index++ {
		var product [39]uint16
		result, _, _ := msiEnumRelatedProductsW.Call(
			uintptr(unsafe.Pointer(upgradeCode)),
			0,
			uintptr(index),
			uintptr(unsafe.Pointer(&product[0])),
		)
		switch uint32(result) {
		case uint32(windows.ERROR_SUCCESS):
			code := strings.ToUpper(windows.UTF16ToString(product[:]))
			if !productCodePattern.MatchString(code) {
				return nil, fmt.Errorf("Windows Installer returned an invalid ZenPlus ProductCode %q", code)
			}
			products = append(products, code)
		case uint32(windows.ERROR_NO_MORE_ITEMS):
			return canonicalProductCodes(products), nil
		default:
			return nil, fmt.Errorf("enumerate related ZenPlus MSI products: Windows Installer error %d", result)
		}
	}
}

func canonicalProductCodes(products []string) []string {
	unique := make(map[string]bool, len(products))
	for _, product := range products {
		product = strings.ToUpper(strings.TrimSpace(product))
		if productCodePattern.MatchString(product) {
			unique[product] = true
		}
	}
	out := make([]string, 0, len(unique))
	for product := range unique {
		out = append(out, product)
	}
	sort.Strings(out)
	return out
}

func legacyMSIUninstallArgs(productCode string) []string {
	return []string{
		"/x", strings.ToUpper(strings.TrimSpace(productCode)),
		"/qn", "/norestart",
		"UPGRADINGPRODUCTCODE=" + agentMSIUpgradeCode,
	}
}

func runLegacyMSIUninstall(productCode string) error {
	if !productCodePattern.MatchString(strings.TrimSpace(productCode)) {
		return fmt.Errorf("invalid ZenPlus MSI ProductCode %q", productCode)
	}
	cmd := exec.Command("msiexec.exe", legacyMSIUninstallArgs(productCode)...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	output, err := cmd.CombinedOutput()
	if err == nil {
		return nil
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		switch exitErr.ExitCode() {
		case 1605, 1614: // already absent / product uninstalled
			return nil
		case 3010: // removed successfully; reboot was requested but suppressed
			return nil
		}
	}
	detail := strings.TrimSpace(string(output))
	if detail != "" {
		return fmt.Errorf("msiexec failed for %s: %w: %s", productCode, err, detail)
	}
	return fmt.Errorf("msiexec failed for %s: %w", productCode, err)
}

func removeLegacyMSIRegistrations(opts options, logf func(options, string, ...any)) error {
	// When this setup is embedded in the new MSI, WiX MajorUpgrade owns related
	// product removal. Recursing into msiexec from its deferred custom action
	// would deadlock the Windows Installer transaction.
	if opts.managedByMSI {
		return nil
	}
	products, err := enumerateRelatedMSIProducts()
	if err != nil {
		return err
	}
	for _, product := range canonicalProductCodes(products) {
		logf(opts, "Removing superseded ZenPlus Agent MSI registration %s", product)
		if err := uninstallLegacyMSIProduct(product); err != nil {
			return fmt.Errorf("remove superseded ZenPlus Agent MSI %s: %w", product, err)
		}
	}
	return nil
}
