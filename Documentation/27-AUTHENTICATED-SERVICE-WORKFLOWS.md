# Authenticated Service Workflows

ZenPlus service checks can monitor applications that require authentication and more than one navigation step. A journey uses one cookie jar from start to finish, so a form login can establish a session before ZenPlus checks protected pages or API endpoints.

## Supported authentication

- **Web form / session:** injects `{{username}}` and `{{password}}` into a form-encoded or JSON login request, then carries the returned cookies into later steps.
- **HTTP Basic:** applies the saved username and password to each request.
- **Bearer / API token:** injects an `Authorization: Bearer` header at probe time.

Credentials are stored separately from service definitions. Secret values are AES-256-GCM encrypted, are never returned by the API after saving, and are decrypted only in poller memory immediately before a request. Exports contain credential metadata and placeholders, never passwords, tokens, cookies, or ciphertext.

## Create a journey

1. Open **Services**, then add or edit an HTTP(S) check.
2. Set the target to the application’s HTTPS origin.
3. Under **Authentication**, select a saved credential or create one. Only administrators can create or rotate secrets.
4. Enable **Multi-step service journey**.
5. Configure the login as step 1. For a form login, choose Form encoded or JSON and keep the generated credential placeholders in the request body.
6. Add two or more protected navigation or health endpoints. Each step can validate an HTTP status and optional response content.
7. Choose the health rule:
   - **ALL / AND:** every step must pass. Use this for complete business-transaction validation.
   - **ANY / OR:** at least one step must pass. Use this only for alternative redundant endpoints.
8. Save, then use **Test now**. The result reports the number of steps passed and names failed steps without exposing response bodies or credentials.

## Security boundaries

- Authenticated checks require HTTPS.
- Authenticated probes validate the server certificate against the appliance trust store; install the internal CA instead of disabling verification.
- Every step in a journey must use the same scheme, host, and effective port. This prevents a workflow from forwarding credentials or session cookies to another origin.
- Cross-origin redirects are rejected for authenticated journeys.
- Credentials cannot be embedded in URLs.
- Request headers reject line breaks, and response bodies are never included in errors or logs.
- A journey supports up to 10 steps; each response content match reads at most 1 MiB.

## Troubleshooting

- **Form authentication requires a multi-step workflow:** add a login step before the protected navigation steps.
- **Login step must inject username and password:** the first form step must contain both `{{username}}` and `{{password}}` in its body or headers.
- **All workflow steps must use the same origin:** keep every URL on the target application’s HTTPS origin. Monitor a separate origin with a separate service check.
- **Expected status mismatch:** include legitimate post-login redirects in the expected status pattern, such as `200,302` or `200-399`.
- **Content validation failed:** choose stable business-health text or a JSON fragment rather than dynamic user-interface content.
