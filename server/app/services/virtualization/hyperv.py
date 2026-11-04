"""Invoke one parameterless read-only command through a restricted JEA endpoint."""
import json
import os
import tempfile
from urllib.parse import urlsplit

from .common import ConnectorError, Snapshot


def collect(config):
    from pypsrp.wsman import WSMan
    from pypsrp.powershell import PowerShell, RunspacePool
    u = urlsplit(config.endpoint)
    ca_path = None
    try:
        if config.ca_pem:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                f.write(config.ca_pem)
                ca_path = f.name
        with WSMan(u.hostname, port=u.port or 5986, ssl=True, auth="ntlm",
                   username=config.username, password=config.secret.get_secret_value(),
                   cert_validation=ca_path or True, connection_timeout=20,
                   read_timeout=60, operation_timeout=50, no_proxy=True) as wsman:
            # pypsrp 0.8.1 uses requests' default redirect policy. Keep authenticated
            # requests on the configured HTTPS origin and disable environment proxies.
            session = wsman.transport._build_session()
            session.trust_env = False
            original_send = session.send
            def send(request, **kwargs):
                kwargs["allow_redirects"] = False
                response = original_send(request, **kwargs)
                if response.is_redirect:
                    raise ConnectorError("Hyper-V redirected the authenticated endpoint; configure its final HTTPS address")
                return response
            session.send = send
            wsman.transport.session = session
            with RunspacePool(wsman, configuration_name="ZenPlusMonitoring") as pool:
                ps = PowerShell(pool)
                result = ps.add_cmdlet("Get-ZenPlusHypervisorSnapshot").invoke()
                if ps.had_errors or len(result) != 1:
                    raise ConnectorError("Hyper-V JEA collector failed; check endpoint installation and permissions")
                body = json.loads(str(result[0]))
        if body.get("schema_version") != 1 or not isinstance(body.get("resources"), list):
            raise ConnectorError("Hyper-V returned an unsupported collector schema")
        snapshot = Snapshot(version=body.get("version", "Hyper-V"), capabilities=body.get("capabilities", {}),
                            warnings=body.get("warnings", []))
        for row in body["resources"]:
            snapshot.resource(row["kind"], row["id"], row["name"], parent_id=row.get("parent_id"),
                              health=row.get("health", "unknown"), power_state=row.get("power_state"),
                              metrics=row.get("metrics", {}))
        snapshot.alerts = body.get("alerts", [])
        return snapshot
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError("Hyper-V collection failed; check WinRM HTTPS trust, monitoring credential and ZenPlusMonitoring JEA endpoint") from None
    finally:
        if ca_path:
            os.unlink(ca_path)
