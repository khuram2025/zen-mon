ZenPlus offline Python tracing support
======================================

Scope
-----
The bundle contains CPython 3.10-3.13 x64 OpenTelemetry packages. It does not
install Python itself and it does not modify an application automatically.
ZenPlus currently accepts OTLP traces only; application metrics and log records
are deliberately disabled until appliance ingest support is available.

Installation
------------
Run PowerShell as the account that owns the application's virtual environment:

  .\Install-ZenPlusPythonTracing.ps1 `
    -PythonPath C:\path\to\venv\Scripts\python.exe `
    -ServiceName orders-api `
    -Environment prod

The script uses only the adjacent wheelhouse (no internet), detects installed
frameworks and clients, installs the matching bundled integrations, runs
`pip check`, and prints the environment variables and launch command.

For Windows services, arrange for the service wrapper to launch the printed
`opentelemetry-instrument.exe ...` command and set the printed OTEL_* variables.
The ZenPlus controller's managed instrumentation
button intentionally supports IIS/.NET, Java, and Node.js services only;
Python setup remains an explicit application-owner action because it installs
packages into the application's own environment.
