package checker

import (
	"context"
	"crypto/x509"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

func workflowCheck(serverURL string) *ServiceCheck {
	credentialID := uuid.New()
	return &ServiceCheck{
		ID:                 uuid.New(),
		CheckType:          "http",
		Timeout:            2 * time.Second,
		CredentialID:       &credentialID,
		CredentialAuthType: "form",
		CredentialUsername: "operator@example.com",
		CredentialSecret:   "p@ss word&safe",
		WorkflowOperator:   "all",
		WorkflowSteps: []HTTPWorkflowStep{
			{
				Name: "Sign in", URL: serverURL + "/login", Method: "POST",
				Headers: map[string]string{"Content-Type": "application/x-www-form-urlencoded"},
				Body:    "username={{username}}&password={{password}}", ExpectedStatuses: "204",
				FollowRedirects: true,
			},
			{
				Name: "Open dashboard", URL: serverURL + "/dashboard", Method: "GET",
				ExpectedStatuses: "200", ContentMatch: "Service healthy", FollowRedirects: true,
			},
		},
	}
}

func checkerForTLSServer(server *httptest.Server) *HTTPChecker {
	pool := x509.NewCertPool()
	pool.AddCert(server.Certificate())
	checker := NewHTTPChecker(zap.NewNop().Sugar())
	checker.rootCAs = pool
	return checker
}

func TestHTTPWorkflowFormLoginKeepsSessionCookie(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/login", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if string(body) != "username=operator%40example.com&password=p%40ss+word%26safe" {
			http.Error(w, "bad credentials", http.StatusUnauthorized)
			return
		}
		http.SetCookie(w, &http.Cookie{Name: "session", Value: "valid", Path: "/", Secure: true})
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/dashboard", func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie("session")
		if err != nil || cookie.Value != "valid" {
			http.Error(w, "login required", http.StatusUnauthorized)
			return
		}
		io.WriteString(w, "Service healthy")
	})
	server := httptest.NewTLSServer(mux)
	defer server.Close()

	result := checkerForTLSServer(server).Check(context.Background(), workflowCheck(server.URL), "test")
	if !result.IsUp {
		t.Fatalf("expected workflow up, got error: %s", result.Error)
	}
}

func TestHTTPWorkflowAllReportsFailingStepWithoutSecret(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/login" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		io.WriteString(w, "not the expected page")
	}))
	defer server.Close()

	check := workflowCheck(server.URL)
	result := checkerForTLSServer(server).Check(context.Background(), check, "test")
	if result.IsUp || !strings.Contains(result.Error, "Open dashboard") {
		t.Fatalf("expected named failing step, got up=%v error=%q", result.IsUp, result.Error)
	}
	if strings.Contains(result.Error, check.CredentialSecret) {
		t.Fatal("result leaked the credential secret")
	}
}

func TestHTTPWorkflowAnyAllowsOneHealthyNavigation(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/login" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
	}))
	defer server.Close()

	check := workflowCheck(server.URL)
	check.WorkflowOperator = "any"
	result := checkerForTLSServer(server).Check(context.Background(), check, "test")
	if !result.IsUp {
		t.Fatalf("expected ANY workflow up, got %s", result.Error)
	}
}

func TestHTTPWorkflowRejectsCrossOriginCredentialUse(t *testing.T) {
	first := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer first.Close()
	second := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	defer second.Close()

	check := workflowCheck(first.URL)
	check.WorkflowSteps[1].URL = second.URL + "/dashboard"
	result := NewHTTPChecker(zap.NewNop().Sugar()).Check(context.Background(), check, "test")
	if result.IsUp || result.Error != "all workflow steps must use the same origin" {
		t.Fatalf("expected same-origin rejection, got up=%v error=%q", result.IsUp, result.Error)
	}
}

func TestHTTPWorkflowCanExplicitlyIgnoreUntrustedCertificate(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/login" {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		io.WriteString(w, "Service healthy")
	}))
	defer server.Close()

	check := workflowCheck(server.URL)
	checker := NewHTTPChecker(zap.NewNop().Sugar())
	failed := checker.Check(context.Background(), check, "test")
	if failed.IsUp {
		t.Fatal("expected certificate verification to reject the untrusted test certificate")
	}

	check.HTTPIgnoreTLSErrors = true
	result := checker.Check(context.Background(), check, "test")
	if !result.IsUp {
		t.Fatalf("expected explicit TLS bypass to allow workflow, got error: %s", result.Error)
	}
}
