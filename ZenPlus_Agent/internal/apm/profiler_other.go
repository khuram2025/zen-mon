//go:build !windows

package apm

func detectProfiler(int32) bool { return false }

type processModuleFacts struct {
	OTelDetected    bool
	DotnetCore      bool
	DotnetFramework bool
}

func inspectProcessModules(int32) processModuleFacts { return processModuleFacts{} }
