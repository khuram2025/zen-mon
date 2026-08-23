-- Windows Integrated Authentication for trusted internal service checks.

ALTER TABLE service_credentials
    DROP CONSTRAINT IF EXISTS service_credentials_auth_type_check;

ALTER TABLE service_credentials
    ADD CONSTRAINT service_credentials_auth_type_check
    CHECK (auth_type IN ('basic', 'bearer', 'form', 'ntlm'));
