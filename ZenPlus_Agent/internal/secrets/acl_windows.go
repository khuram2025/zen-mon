//go:build windows

package secrets

import (
	cryptorand "crypto/rand"
	"crypto/sha1"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	zenPlusServiceName    = "ZenPlusAgent"
	machineDataSDDL       = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;%s)"
	machineSecretSDDL     = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;%s)"
	machineDashboardSDDL  = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;%s)(A;OICI;GRGX;;;BU)"
	dpapiFilenameMarker   = ".dpapi"
	dashboardDirectory    = "AgentDashboard"
	dashboardSnapshotName = "snapshot.json"
)

type machineACL struct {
	descriptor *windows.SECURITY_DESCRIPTOR
	owner      *windows.SID
	dacl       *windows.ACL
}

type machineACLSet struct {
	data      *machineACL
	secret    *machineACL
	dashboard *machineACL
}

// HardenMachineDataTree restricts the machine data tree to SYSTEM,
// administrators, and the agent service SID. The desktop dashboard receives a
// separately generated, sanitized status snapshot; raw spool data, APM queues,
// logs, rollback state, and credentials never need ordinary-user access.
func HardenMachineDataTree(root, serviceName string) error {
	acls, err := newMachineACLSet(serviceName)
	if err != nil {
		return err
	}
	return hardenMachineDataTreeWith(root, acls, applyMachineACL)
}

func newMachineACLSet(serviceName string) (*machineACLSet, error) {
	serviceSID, err := serviceSIDString(serviceName)
	if err != nil {
		return nil, err
	}
	data, err := newMachineACL(fmt.Sprintf(machineDataSDDL, serviceSID))
	if err != nil {
		return nil, fmt.Errorf("build machine data ACL: %w", err)
	}
	secret, err := newMachineACL(fmt.Sprintf(machineSecretSDDL, serviceSID))
	if err != nil {
		return nil, fmt.Errorf("build machine secret ACL: %w", err)
	}
	dashboard, err := newMachineACL(fmt.Sprintf(machineDashboardSDDL, serviceSID))
	if err != nil {
		return nil, fmt.Errorf("build machine dashboard ACL: %w", err)
	}
	return &machineACLSet{data: data, secret: secret, dashboard: dashboard}, nil
}

// PrepareMachineDashboardDirectory creates the one intentionally public
// machine-status directory with a protected DACL. Ordinary users can list and
// read its sanitized snapshot, but cannot create, replace, or delete content.
func PrepareMachineDashboardDirectory(path string) error {
	if !isMachineDashboardDirectory(path) {
		return fmt.Errorf("machine dashboard directory is not canonical: %q", path)
	}
	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		return err
	}
	parent := filepath.Dir(path)
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create machine dashboard parent: %w", err)
	}
	if err := validateDashboardPathType(parent, true); err != nil {
		return fmt.Errorf("validate machine dashboard parent: %w", err)
	}
	encoded, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return fmt.Errorf("encode machine dashboard directory: %w", err)
	}
	attributes := windows.SecurityAttributes{
		Length:             uint32(unsafe.Sizeof(windows.SecurityAttributes{})),
		SecurityDescriptor: acls.dashboard.descriptor,
	}
	err = windows.CreateDirectory(encoded, &attributes)
	runtime.KeepAlive(acls.dashboard.descriptor)
	if err != nil && err != windows.ERROR_ALREADY_EXISTS {
		return fmt.Errorf("create machine dashboard directory: %w", err)
	}
	if err := validateDashboardPathType(path, true); err != nil {
		return err
	}
	if err := applyMachineACL(path, acls.dashboard); err != nil {
		return fmt.Errorf("protect machine dashboard directory: %w", err)
	}
	return nil
}

// ProtectMachineDashboardFile gives a freshly written snapshot the same
// explicit, non-inherited read-only policy as its directory before publish.
func ProtectMachineDashboardFile(path string) error {
	if !isMachineDashboardTemporaryPath(path) {
		return fmt.Errorf("machine dashboard temporary file is not canonical: %q", path)
	}
	if err := validateDashboardPathType(path, false); err != nil {
		return err
	}
	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		return err
	}
	if err := applyMachineACL(path, acls.dashboard); err != nil {
		return fmt.Errorf("protect machine dashboard file: %w", err)
	}
	return nil
}

// ValidateMachineDashboardSnapshot prevents the unelevated UI from trusting a
// file planted before service startup or redirected through a reparse point.
func ValidateMachineDashboardSnapshot(path string) error {
	if !isMachineDashboardSnapshotPath(path) {
		return fmt.Errorf("machine dashboard snapshot is not canonical: %q", path)
	}
	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		return err
	}
	if err := validateDashboardPathType(filepath.Dir(filepath.Dir(path)), true); err != nil {
		return fmt.Errorf("validate machine dashboard parent: %w", err)
	}
	for _, item := range []struct {
		path      string
		directory bool
	}{
		{path: filepath.Dir(path), directory: true},
		{path: path, directory: false},
	} {
		if err := validateDashboardPathType(item.path, item.directory); err != nil {
			return err
		}
		if err := validateMachineACL(item.path, acls.dashboard); err != nil {
			return fmt.Errorf("untrusted machine dashboard path %q: %w", item.path, err)
		}
	}
	return nil
}

func newMachineACL(sddl string) (*machineACL, error) {
	descriptor, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return nil, err
	}
	owner, _, err := descriptor.Owner()
	if err != nil {
		return nil, err
	}
	dacl, _, err := descriptor.DACL()
	if err != nil {
		return nil, err
	}
	return &machineACL{descriptor: descriptor, owner: owner, dacl: dacl}, nil
}

func hardenMachineDataTreeWith(root string, acls *machineACLSet, apply func(string, *machineACL) error) error {
	if acls == nil || !completeMachineACL(acls.data) || !completeMachineACL(acls.secret) {
		return fmt.Errorf("machine data ACL set is incomplete")
	}
	if strings.TrimSpace(root) == "" || !filepath.IsAbs(root) {
		return fmt.Errorf("machine data path must be absolute: %q", root)
	}
	root = filepath.Clean(root)
	if root == filepath.VolumeName(root)+string(filepath.Separator) {
		return fmt.Errorf("refusing to harden volume root %q", root)
	}

	paths := make([]string, 0, 16)
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		attributes, err := windows.GetFileAttributes(windows.StringToUTF16Ptr(path))
		if err != nil {
			return fmt.Errorf("inspect %q before ACL migration: %w", path, err)
		}
		if attributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
			return fmt.Errorf("refusing to apply machine data ACL through reparse point %q", path)
		}
		paths = append(paths, path)
		return nil
	})
	if err != nil {
		return fmt.Errorf("enumerate machine data tree %q: %w", root, err)
	}

	// Existing descendants may still rely on legacy inherited permissions.
	// Process the deepest paths first, then protect the root.
	sort.SliceStable(paths, func(i, j int) bool {
		return pathDepth(paths[i]) > pathDepth(paths[j])
	})
	for _, path := range paths {
		acl := acls.data
		if isDPAPISecretPath(path) {
			acl = acls.secret
		}
		if err := apply(path, acl); err != nil {
			return fmt.Errorf("protect machine data path %q: %w", path, err)
		}
	}
	return nil
}

func completeMachineACL(acl *machineACL) bool {
	return acl != nil && acl.descriptor != nil && acl.owner != nil && acl.dacl != nil
}

func applyMachineACL(path string, acl *machineACL) error {
	err := windows.SetNamedSecurityInfo(
		path,
		windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|
			windows.DACL_SECURITY_INFORMATION|
			windows.PROTECTED_DACL_SECURITY_INFORMATION,
		acl.owner,
		nil,
		acl.dacl,
		nil,
	)
	runtime.KeepAlive(acl.descriptor)
	return err
}

// writeMachineSecretFile creates the temporary file with its protected DACL
// already attached. That avoids a disclosure window in which another local
// user could open a permissive file before its ACL is tightened.
func writeMachineSecretFile(path string, contents []byte) error {
	acls, err := newMachineACLSet(zenPlusServiceName)
	if err != nil {
		return err
	}
	directory := filepath.Dir(path)
	var tempPath string
	var file *os.File
	for attempt := 0; attempt < 20; attempt++ {
		random := make([]byte, 16)
		if _, err := cryptorand.Read(random); err != nil {
			return fmt.Errorf("generate protected credential filename: %w", err)
		}
		tempPath = filepath.Join(directory, "."+filepath.Base(path)+".new-"+hex.EncodeToString(random))
		name, err := windows.UTF16PtrFromString(tempPath)
		if err != nil {
			return fmt.Errorf("encode protected credential filename: %w", err)
		}
		attributes := windows.SecurityAttributes{
			Length:             uint32(unsafe.Sizeof(windows.SecurityAttributes{})),
			SecurityDescriptor: acls.secret.descriptor,
		}
		handle, err := windows.CreateFile(
			name,
			windows.GENERIC_READ|windows.GENERIC_WRITE,
			0,
			&attributes,
			windows.CREATE_NEW,
			windows.FILE_ATTRIBUTE_NORMAL,
			0,
		)
		runtime.KeepAlive(acls.secret.descriptor)
		if err == nil {
			file = os.NewFile(uintptr(handle), tempPath)
			if file == nil {
				_ = windows.CloseHandle(handle)
				return fmt.Errorf("open protected credential temporary file")
			}
			break
		}
		if err != windows.ERROR_FILE_EXISTS && err != windows.ERROR_ALREADY_EXISTS {
			return fmt.Errorf("create protected credential temporary file: %w", err)
		}
	}
	if file == nil {
		return fmt.Errorf("create unique protected credential temporary file")
	}
	defer os.Remove(tempPath)
	if _, err := file.Write(contents); err != nil {
		_ = file.Close()
		return fmt.Errorf("write protected credential temporary file: %w", err)
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		return fmt.Errorf("flush protected credential temporary file: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close protected credential temporary file: %w", err)
	}
	if err := os.Rename(tempPath, path); err != nil {
		return fmt.Errorf("replace protected credential file: %w", err)
	}
	return nil
}

func isMachineDataPath(path string) bool {
	programData := strings.TrimSpace(os.Getenv("ProgramData"))
	if programData == "" {
		programData = `C:\ProgramData`
	}
	machineRoot := filepath.Join(programData, "ZenPlus", "Agent")
	absRoot, err := filepath.Abs(machineRoot)
	if err != nil {
		return false
	}
	absPath, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	relative, err := filepath.Rel(absRoot, absPath)
	if err != nil {
		return false
	}
	return relative == "." || (!filepath.IsAbs(relative) && relative != ".." && !strings.HasPrefix(relative, ".."+string(filepath.Separator)))
}

func isMachineDashboardDirectory(path string) bool {
	return sameMachinePath(path, machineDashboardDirectoryPath())
}

func isMachineDashboardSnapshotPath(path string) bool {
	return sameMachinePath(path, filepath.Join(machineDashboardDirectoryPath(), dashboardSnapshotName))
}

func isMachineDashboardTemporaryPath(path string) bool {
	if !sameMachinePath(filepath.Dir(path), machineDashboardDirectoryPath()) {
		return false
	}
	name := strings.ToLower(filepath.Base(path))
	return strings.HasPrefix(name, ".snapshot-") && strings.HasSuffix(name, ".json")
}

func machineDashboardDirectoryPath() string {
	programData := strings.TrimSpace(os.Getenv("ProgramData"))
	if programData == "" {
		programData = `C:\ProgramData`
	}
	return filepath.Join(programData, "ZenPlus", dashboardDirectory)
}

func sameMachinePath(left, right string) bool {
	leftAbsolute, leftErr := filepath.Abs(left)
	rightAbsolute, rightErr := filepath.Abs(right)
	if leftErr != nil || rightErr != nil {
		return false
	}
	return strings.EqualFold(filepath.Clean(leftAbsolute), filepath.Clean(rightAbsolute))
}

func validateDashboardPathType(path string, directory bool) error {
	encoded, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return fmt.Errorf("encode machine dashboard path: %w", err)
	}
	attributes, err := windows.GetFileAttributes(encoded)
	if err != nil {
		return fmt.Errorf("inspect machine dashboard path %q: %w", path, err)
	}
	if attributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0 {
		return fmt.Errorf("machine dashboard path is a reparse point: %q", path)
	}
	isDirectory := attributes&windows.FILE_ATTRIBUTE_DIRECTORY != 0
	if isDirectory != directory {
		if directory {
			return fmt.Errorf("machine dashboard path is not a directory: %q", path)
		}
		return fmt.Errorf("machine dashboard path is not a regular file: %q", path)
	}
	return nil
}

func validateMachineACL(path string, expected *machineACL) error {
	if !completeMachineACL(expected) {
		return fmt.Errorf("expected machine ACL is incomplete")
	}
	descriptor, err := windows.GetNamedSecurityInfo(
		path,
		windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION,
	)
	if err != nil {
		return fmt.Errorf("read security descriptor: %w", err)
	}
	owner, _, err := descriptor.Owner()
	if err != nil {
		return fmt.Errorf("read owner: %w", err)
	}
	if owner == nil || !owner.Equals(expected.owner) {
		return fmt.Errorf("owner does not match the protected dashboard policy")
	}
	control, _, err := descriptor.Control()
	if err != nil {
		return fmt.Errorf("read security descriptor control: %w", err)
	}
	if control&windows.SE_DACL_PROTECTED == 0 {
		return fmt.Errorf("DACL inherits from its parent")
	}
	dacl, _, err := descriptor.DACL()
	if err != nil {
		return fmt.Errorf("read DACL: %w", err)
	}
	match, err := equalMachineACL(dacl, expected.dacl)
	if err != nil {
		return err
	}
	runtime.KeepAlive(descriptor)
	if !match {
		return fmt.Errorf("DACL does not match the read-only dashboard policy")
	}
	return nil
}

func equalMachineACL(left, right *windows.ACL) (bool, error) {
	if left == nil || right == nil || left.AceCount != right.AceCount {
		return false, nil
	}
	for index := uint32(0); index < uint32(left.AceCount); index++ {
		var leftACE, rightACE *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(left, index, &leftACE); err != nil {
			return false, fmt.Errorf("read dashboard DACL entry %d: %w", index, err)
		}
		if err := windows.GetAce(right, index, &rightACE); err != nil {
			return false, fmt.Errorf("read expected dashboard DACL entry %d: %w", index, err)
		}
		if leftACE == nil || rightACE == nil ||
			leftACE.Header.AceType != rightACE.Header.AceType ||
			leftACE.Mask != rightACE.Mask {
			return false, nil
		}
		leftSID := (*windows.SID)(unsafe.Pointer(&leftACE.SidStart))
		rightSID := (*windows.SID)(unsafe.Pointer(&rightACE.SidStart))
		if !leftSID.Equals(rightSID) {
			return false, nil
		}
	}
	return true, nil
}

func isDPAPISecretPath(path string) bool {
	return strings.Contains(strings.ToLower(filepath.Base(path)), dpapiFilenameMarker)
}

func pathDepth(path string) int {
	path = filepath.Clean(path)
	return strings.Count(strings.TrimPrefix(path, filepath.VolumeName(path)), string(filepath.Separator))
}

// Windows service SIDs are SHA-1(service-name-in-uppercase-UTF-16LE), split
// into five little-endian uint32 subauthorities under NT SERVICE (S-1-5-80).
func serviceSIDString(serviceName string) (string, error) {
	serviceName = strings.TrimSpace(serviceName)
	if serviceName == "" {
		return "", fmt.Errorf("Windows service name is empty")
	}
	encoded := utf16.Encode([]rune(strings.ToUpper(serviceName)))
	bytes := make([]byte, len(encoded)*2)
	for i, value := range encoded {
		binary.LittleEndian.PutUint16(bytes[i*2:], value)
	}
	hash := sha1.Sum(bytes) // #nosec G505 -- required by the Windows service SID algorithm.
	return fmt.Sprintf(
		"S-1-5-80-%d-%d-%d-%d-%d",
		binary.LittleEndian.Uint32(hash[0:4]),
		binary.LittleEndian.Uint32(hash[4:8]),
		binary.LittleEndian.Uint32(hash[8:12]),
		binary.LittleEndian.Uint32(hash[12:16]),
		binary.LittleEndian.Uint32(hash[16:20]),
	), nil
}
