"""Run a PowerShell verification script over WinRM/NTLM.

pywinrm is intentionally loaded from a caller-provided temporary dependency
directory so release testing does not add a runtime dependency to ZenPlus.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import os
import sys
import uuid
from pathlib import Path
from pathlib import PureWindowsPath


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--script", type=Path)
    source.add_argument(
        "--script-env",
        help="read the PowerShell body from this environment variable",
    )
    source.add_argument(
        "--command",
        help="run a native command through cmd.exe (avoids PowerShell/CLR)",
    )
    parser.add_argument(
        "--upload",
        action="append",
        default=[],
        metavar="LOCAL::REMOTE",
        help="upload a file in bounded WinRM chunks; may be repeated",
    )
    args = parser.parse_args()

    if not (args.script or args.script_env or args.command or args.upload):
        parser.error("one command source or at least one --upload is required")

    dependency_path = os.environ.get("ZENPLUS_PYWINRM_PATH")
    password = os.environ.get("ZENPLUS_WINRM_PASSWORD")
    if not dependency_path or not password:
        parser.error("ZENPLUS_PYWINRM_PATH and ZENPLUS_WINRM_PASSWORD are required")
    sys.path.insert(0, dependency_path)

    import winrm  # type: ignore[import-not-found]

    script = ""
    if args.script:
        script = args.script.read_text(encoding="utf-8")
    elif args.script_env:
        script = os.environ.get(args.script_env or "", "")
        if not script:
            parser.error(f"environment variable {args.script_env!r} is empty")
    session = winrm.Session(
        f"http://{args.host}:5985/wsman",
        auth=(args.user, password),
        transport="ntlm",
        read_timeout_sec=180,
        operation_timeout_sec=170,
    )

    def emit_result(result) -> int:
        sys.stdout.write(result.std_out.decode(errors="replace"))
        sys.stderr.write(result.std_err.decode(errors="replace"))
        return int(result.status_code)

    for mapping in args.upload:
        if "::" not in mapping:
            parser.error("--upload must use LOCAL::REMOTE")
        local_text, remote = mapping.split("::", 1)
        local = Path(local_text).resolve()
        if not local.is_file():
            parser.error(f"upload source is not a file: {local}")
        if not PureWindowsPath(remote).is_absolute():
            parser.error(f"upload destination must be an absolute Windows path: {remote}")

        encoded = base64.b64encode(local.read_bytes()).decode("ascii")
        remote_escaped = remote.replace("'", "''")
        staging = rf"C:\Windows\Temp\zenplus-winrm-{uuid.uuid4().hex}.b64"
        staging_escaped = staging.replace("'", "''")
        init = (
            f"$d='{remote_escaped}';"
            "$p=[IO.Path]::GetDirectoryName($d);"
            "[IO.Directory]::CreateDirectory($p)|Out-Null;"
            f"[IO.File]::WriteAllText('{staging_escaped}','')"
        )
        status = emit_result(session.run_ps(init))
        if status:
            return status
        # pywinrm UTF-16/base64 encodes each PowerShell invocation again. Keep
        # each raw chunk small enough for hosts enforcing cmd.exe's 8191-byte
        # command-line ceiling.
        for offset in range(0, len(encoded), 700):
            chunk = encoded[offset : offset + 700]
            append = (
                f"[IO.File]::AppendAllText('{staging_escaped}','{chunk}',"
                "[Text.Encoding]::ASCII)"
            )
            status = emit_result(session.run_ps(append))
            if status:
                return status
        finish = (
            f"$s='{staging_escaped}';$d='{remote_escaped}';"
            "try{[IO.File]::WriteAllBytes($d,[Convert]::FromBase64String("
            "[IO.File]::ReadAllText($s)))}finally{Remove-Item -LiteralPath $s -Force}"
        )
        status = emit_result(session.run_ps(finish))
        if status:
            return status
        sys.stdout.write(f"uploaded {local.name} -> {remote}\n")

    if not (args.command or args.script or args.script_env):
        return 0

    if args.command:
        result = session.run_cmd("cmd.exe", ["/d", "/s", "/c", args.command])
    else:
        payload = base64.b64encode(gzip.compress(script.encode("utf-8"))).decode("ascii")
        remote_script = (
            f"$b='{payload}';"
            "$m=New-Object IO.MemoryStream(,[Convert]::FromBase64String($b));"
            "$g=New-Object IO.Compression.GzipStream($m,[IO.Compression.CompressionMode]::Decompress);"
            "$r=New-Object IO.StreamReader($g);"
            "& ([ScriptBlock]::Create($r.ReadToEnd()))"
        )
        result = session.run_ps(remote_script)
    return emit_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
