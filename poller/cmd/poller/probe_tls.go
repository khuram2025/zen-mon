package main

import (
	"context"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"time"

	"github.com/zenplus/poller/internal/checker"
)

// A small, credential-free bridge lets on-demand Python probes use exactly the
// same verifier as scheduled probes. No database or daemon startup is needed.
func verifyServiceTLS(input io.Reader, output io.Writer) error {
	var request struct {
		Host    string                    `json:"host"`
		Port    int                       `json:"port"`
		Timeout int                       `json:"timeout"`
		Policy  *checker.ProbeTrustPolicy `json:"policy"`
	}
	if err := json.NewDecoder(io.LimitReader(input, 3*1024*1024)).Decode(&request); err != nil {
		return fmt.Errorf("invalid TLS verification request")
	}
	if request.Timeout < 1 || request.Timeout > 60 || request.Port < 1 || request.Port > 65535 || request.Host == "" {
		return fmt.Errorf("invalid TLS verification target")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(request.Timeout)*time.Second)
	defer cancel()
	cert, err := checker.VerifyServiceTLS(ctx, request.Host, request.Port, request.Policy)
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(map[string]string{"pem": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cert.Raw}))})
}
