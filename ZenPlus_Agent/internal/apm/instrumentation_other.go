//go:build !windows

package apm

import (
	"context"
	"fmt"
)

func applyIISInstrumentation(context.Context, string, string, InstrumentationRequest) (InstrumentationResult, error) {
	return InstrumentationResult{}, fmt.Errorf("IIS instrumentation is supported only by the Windows agent")
}

func applyWindowsServiceInstrumentation(context.Context, string, string, InstrumentationRequest) (InstrumentationResult, error) {
	return InstrumentationResult{}, fmt.Errorf("Windows-service instrumentation is supported only by the Windows agent")
}
