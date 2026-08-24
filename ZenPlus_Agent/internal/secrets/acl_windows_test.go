//go:build windows

package secrets

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

func TestServiceSIDStringMatchesWindowsServiceSIDAlgorithm(t *testing.T) {
	got, err := serviceSIDString("TrustedInstaller")
	if err != nil {
		t.Fatalf("serviceSIDString: %v", err)
	}
	const want = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
	if got != want {
		t.Fatalf("service SID = %q, want %q", got, want)
	}
}

func TestMachineACLsProtectAllServiceData(t *testing.T) {
	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		t.Fatalf("newMachineACLSet: %v", err)
	}
	serviceSID, err := serviceSIDString(zenPlusServiceName)
	if err != nil {
		t.Fatalf("serviceSIDString: %v", err)
	}
	for name, acl := range map[string]*machineACL{"data": acls.data, "secret": acls.secret, "dashboard": acls.dashboard} {
		control, _, err := acl.descriptor.Control()
		if err != nil {
			t.Fatalf("read %s descriptor control: %v", name, err)
		}
		if control&windows.SE_DACL_PROTECTED == 0 {
			t.Errorf("%s DACL is not protected from parent inheritance", name)
		}
		if rendered := acl.descriptor.String(); !strings.Contains(rendered, serviceSID) {
			t.Errorf("%s DACL %q does not grant service SID %s", name, rendered, serviceSID)
		}
	}
	if acls.data.dacl.AceCount != 3 {
		t.Fatalf("data DACL ACE count = %d, want 3", acls.data.dacl.AceCount)
	}
	if acls.secret.dacl.AceCount != 3 {
		t.Fatalf("secret DACL ACE count = %d, want 3", acls.secret.dacl.AceCount)
	}
	if acls.dashboard.dacl.AceCount != 4 {
		t.Fatalf("dashboard DACL ACE count = %d, want 4", acls.dashboard.dacl.AceCount)
	}
	for name, rendered := range map[string]string{
		"data":   acls.data.descriptor.String(),
		"secret": acls.secret.descriptor.String(),
	} {
		if containsBuiltinUsers(rendered) {
			t.Fatalf("%s DACL %q grants BUILTIN\\Users access", name, rendered)
		}
	}
	if rendered := acls.dashboard.descriptor.String(); !containsBuiltinUsers(rendered) {
		t.Fatalf("dashboard DACL %q does not grant BUILTIN\\Users read access", rendered)
	}
}

func TestHardenMachineDataTreeMigratesDPAPIVariants(t *testing.T) {
	root := t.TempDir()
	state := filepath.Join(root, "state")
	if err := os.MkdirAll(state, 0o755); err != nil {
		t.Fatalf("create state directory: %v", err)
	}
	paths := []string{
		filepath.Join(state, "status.json"),
		filepath.Join(state, "credential.dpapi"),
		filepath.Join(state, "pending-secret.dpapi.new"),
		filepath.Join(state, ".apm-credential.dpapi.tmp-123"),
	}
	for _, path := range paths {
		if err := os.WriteFile(path, []byte("test"), 0o600); err != nil {
			t.Fatalf("create %s: %v", path, err)
		}
	}

	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		t.Fatalf("newMachineACLSet: %v", err)
	}
	applied := make(map[string]*machineACL)
	var order []string
	err = hardenMachineDataTreeWith(root, acls, func(path string, acl *machineACL) error {
		path = filepath.Clean(path)
		applied[path] = acl
		order = append(order, path)
		return nil
	})
	if err != nil {
		t.Fatalf("hardenMachineDataTreeWith: %v", err)
	}
	if got := applied[filepath.Clean(paths[0])]; got != acls.data {
		t.Error("ordinary status data did not receive the protected service-data ACL")
	}
	for _, path := range paths[1:] {
		if got := applied[filepath.Clean(path)]; got != acls.secret {
			t.Errorf("DPAPI path %q did not receive the protected secret ACL", path)
		}
	}
	if len(order) == 0 || order[len(order)-1] != filepath.Clean(root) {
		t.Fatalf("root must be protected last; migration order: %v", order)
	}
}

func TestMachineDataPathIsScopedToProgramDataAgentRoot(t *testing.T) {
	programData := filepath.Join(t.TempDir(), "ProgramData")
	t.Setenv("ProgramData", programData)
	inside := filepath.Join(programData, "ZenPlus", "Agent", "state", "credential.dpapi")
	outside := filepath.Join(programData, "ZenPlus", "Other", "credential.dpapi")
	if !isMachineDataPath(inside) {
		t.Fatalf("machine credential path %q was not recognized", inside)
	}
	if isMachineDataPath(outside) {
		t.Fatalf("path outside the machine agent root %q was accepted", outside)
	}
}

func TestMachineDashboardPathsAreStrictlyScoped(t *testing.T) {
	programData := filepath.Join(t.TempDir(), "ProgramData")
	t.Setenv("ProgramData", programData)
	directory := filepath.Join(programData, "ZenPlus", dashboardDirectory)
	if !isMachineDashboardDirectory(directory) {
		t.Fatalf("canonical dashboard directory %q was rejected", directory)
	}
	if !isMachineDashboardSnapshotPath(filepath.Join(directory, dashboardSnapshotName)) {
		t.Fatal("canonical dashboard snapshot was rejected")
	}
	if !isMachineDashboardTemporaryPath(filepath.Join(directory, ".snapshot-123.json")) {
		t.Fatal("canonical dashboard temporary file was rejected")
	}
	if isMachineDashboardTemporaryPath(filepath.Join(directory, "snapshot.json")) {
		t.Fatal("published snapshot was accepted as a temporary file")
	}
	if isMachineDashboardDirectory(filepath.Join(programData, "ZenPlus", "Agent")) {
		t.Fatal("protected agent data directory was accepted as the public dashboard directory")
	}
}

func TestMachineCredentialFileACLIntegration(t *testing.T) {
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("setting a production machine-scope owner/DACL requires an elevated Windows token")
	}
	programData := filepath.Join(t.TempDir(), "ProgramData")
	t.Setenv("ProgramData", programData)
	root := filepath.Join(programData, "ZenPlus", "Agent")
	state := filepath.Join(root, "state")
	if err := os.MkdirAll(state, 0o755); err != nil {
		t.Fatalf("create machine state directory: %v", err)
	}
	statusPath := filepath.Join(state, "status.json")
	legacySecret := filepath.Join(state, "pending-secret.dpapi")
	if err := os.WriteFile(statusPath, []byte("{}"), 0o600); err != nil {
		t.Fatalf("create status file: %v", err)
	}
	if err := os.WriteFile(legacySecret, []byte("legacy"), 0o600); err != nil {
		t.Fatalf("create legacy secret file: %v", err)
	}
	if err := HardenMachineDataTree(root, zenPlusServiceName); err != nil {
		t.Fatalf("HardenMachineDataTree: %v", err)
	}
	assertBuiltinUsersAccess(t, statusPath, false)
	assertBuiltinUsersAccess(t, legacySecret, false)
	dashboardDir := filepath.Join(programData, "ZenPlus", dashboardDirectory)
	if err := PrepareMachineDashboardDirectory(dashboardDir); err != nil {
		t.Fatalf("PrepareMachineDashboardDirectory: %v", err)
	}
	dashboardTemp := filepath.Join(dashboardDir, ".snapshot-test.json")
	if err := os.WriteFile(dashboardTemp, []byte("{}"), 0o600); err != nil {
		t.Fatalf("create dashboard snapshot: %v", err)
	}
	if err := ProtectMachineDashboardFile(dashboardTemp); err != nil {
		t.Fatalf("ProtectMachineDashboardFile: %v", err)
	}
	dashboardPath := filepath.Join(dashboardDir, dashboardSnapshotName)
	if err := os.Rename(dashboardTemp, dashboardPath); err != nil {
		t.Fatalf("publish dashboard snapshot: %v", err)
	}
	if err := ValidateMachineDashboardSnapshot(dashboardPath); err != nil {
		t.Fatalf("ValidateMachineDashboardSnapshot: %v", err)
	}
	assertBuiltinUsersAccess(t, dashboardPath, true)

	credentialPath := filepath.Join(state, "credential.dpapi")
	want := []byte("zpa_key_test")
	if err := ProtectToFile(credentialPath, want); err != nil {
		t.Fatalf("ProtectToFile: %v", err)
	}
	assertBuiltinUsersAccess(t, credentialPath, false)
	got, err := UnprotectFromFile(credentialPath)
	if err != nil {
		t.Fatalf("UnprotectFromFile: %v", err)
	}
	if string(got) != string(want) {
		t.Fatalf("unprotected credential = %q, want %q", got, want)
	}
}

func assertBuiltinUsersAccess(t *testing.T, path string, want bool) {
	t.Helper()
	descriptor, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatalf("read DACL for %s: %v", path, err)
	}
	if got := containsBuiltinUsers(descriptor.String()); got != want {
		t.Fatalf("BUILTIN\\Users presence for %s = %t, want %t; DACL: %s", path, got, want, descriptor.String())
	}
}

func containsBuiltinUsers(sddl string) bool {
	return strings.Contains(sddl, "S-1-5-32-545") || strings.Contains(sddl, ";;;BU)")
}
