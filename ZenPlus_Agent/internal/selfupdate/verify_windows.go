//go:build windows

package selfupdate

import (
	"fmt"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	modwintrust                        = windows.NewLazySystemDLL("wintrust.dll")
	procWTHelperProvDataFromStateData  = modwintrust.NewProc("WTHelperProvDataFromStateData")
	procWTHelperGetProvSignerFromChain = modwintrust.NewProc("WTHelperGetProvSignerFromChain")
)

// cryptProviderSgnr mirrors the leading fields of CRYPT_PROVIDER_SGNR
// (wintrust.h). Only ChainContext is read; the rest keeps the x64 layout.
type cryptProviderSgnr struct {
	CbStruct          uint32
	VerifyAsOf        windows.Filetime
	CsCertChain       uint32
	PasCertChain      uintptr
	SignerType        uint32
	PsSigner          uintptr
	Error             uint32
	CsCounterSigners  uint32
	PasCounterSigners uintptr
	ChainContext      *windows.CertChainContext
}

func verifyPackageSignature(path string) error {
	return verifyPackageSignatureWithPolicy(path, defaultSignerPolicy())
}

func verifyPackageSignatureWithPolicy(path string, policy signerPolicy) error {
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return fmt.Errorf("encode installer path: %w", err)
	}
	file := windows.WinTrustFileInfo{FilePath: name}
	file.Size = uint32(unsafe.Sizeof(file))
	data := windows.WinTrustData{
		UIChoice:                        windows.WTD_UI_NONE,
		RevocationChecks:                windows.WTD_REVOKE_WHOLECHAIN,
		UnionChoice:                     windows.WTD_CHOICE_FILE,
		FileOrCatalogOrBlobOrSgnrOrCert: unsafe.Pointer(&file),
		StateAction:                     windows.WTD_STATEACTION_VERIFY,
		ProvFlags:                       windows.WTD_REVOCATION_CHECK_CHAIN_EXCLUDE_ROOT,
	}
	data.Size = uint32(unsafe.Sizeof(data))
	// Windows checks Authenticode, the certificate chain, revocation and the
	// timestamp. Do not impose lifetime signing: timestamped releases must
	// remain valid after the short-lived signing certificate expires.
	err = windows.WinVerifyTrustEx(windows.InvalidHWND, &windows.WINTRUST_ACTION_GENERIC_VERIFY_V2, &data)
	var chainErr error
	if err == nil {
		// The provider state is only valid until the CLOSE call below.
		var chain [][]byte
		chain, chainErr = verifiedSignerChain(data.StateData)
		if chainErr == nil {
			chainErr = checkSignerChain(policy, chain)
		}
	}
	data.StateAction = windows.WTD_STATEACTION_CLOSE
	_ = windows.WinVerifyTrustEx(windows.InvalidHWND, &windows.WINTRUST_ACTION_GENERIC_VERIFY_V2, &data)
	runtime.KeepAlive(name)
	runtime.KeepAlive(file)
	if err != nil {
		return fmt.Errorf("Windows does not trust this installer: %w", err)
	}
	if chainErr != nil {
		return fmt.Errorf("installer publisher is not trusted: %w", chainErr)
	}
	return nil
}

// verifiedSignerChain copies the primary signer's certificate chain, as built
// and validated by WinVerifyTrust, leaf first.
func verifiedSignerChain(state windows.Handle) ([][]byte, error) {
	if err := procWTHelperGetProvSignerFromChain.Find(); err != nil {
		return nil, err
	}
	provData, _, _ := procWTHelperProvDataFromStateData.Call(uintptr(state))
	if provData == 0 {
		return nil, fmt.Errorf("signature provider data is unavailable")
	}
	signerPtr, _, _ := procWTHelperGetProvSignerFromChain.Call(provData, 0, 0, 0)
	if signerPtr == 0 {
		return nil, fmt.Errorf("primary signer is unavailable")
	}
	// wintrust owns this memory for the lifetime of the state handle.
	signer := *(**cryptProviderSgnr)(unsafe.Pointer(&signerPtr))
	ctx := signer.ChainContext
	if ctx == nil || ctx.ChainCount == 0 || ctx.Chains == nil {
		return nil, fmt.Errorf("signer certificate chain is unavailable")
	}
	simple := *ctx.Chains
	if simple == nil || simple.NumElements == 0 || simple.Elements == nil {
		return nil, fmt.Errorf("signer certificate chain is empty")
	}
	elements := unsafe.Slice(simple.Elements, simple.NumElements)
	chain := make([][]byte, 0, len(elements))
	for _, element := range elements {
		if element == nil || element.CertContext == nil || element.CertContext.EncodedCert == nil {
			return nil, fmt.Errorf("signer certificate chain has an empty element")
		}
		cert := element.CertContext
		der := unsafe.Slice(cert.EncodedCert, cert.Length)
		chain = append(chain, append([]byte(nil), der...))
	}
	return chain, nil
}
