//go:build windows

package selfupdate

import (
	"fmt"
	"os/exec"
	"strings"
	"syscall"
)

func verifyPackageSignature(path string) error {
	script := `$signature = Get-AuthenticodeSignature -LiteralPath $args[0]; if ($signature.Status -ne 'Valid') { Write-Error ("Authenticode status: " + $signature.Status); exit 1 }; Write-Output $signature.SignerCertificate.Subject`
	cmd := exec.Command("powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script, path)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("Windows does not trust this publisher (%s)", strings.TrimSpace(string(output)))
	}
	return nil
}
