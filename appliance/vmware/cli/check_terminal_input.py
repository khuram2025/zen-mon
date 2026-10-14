#!/usr/bin/env python3
"""Real PTY regression checks; synthetic input only, no credential changes."""
import fcntl
import hashlib
import json
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == '--child':
    sys.path.insert(0, os.environ['CLI_INPUT_SOURCE'])
    import cli
    fd = sys.stdin.fileno()
    initial = termios.tcgetattr(fd)
    initial[3] &= ~termios.ECHO  # Reproduce the inherited no-echo console.
    termios.tcsetattr(fd, termios.TCSANOW, initial)
    mode = sys.argv[2]
    if os.environ.get('CLI_DISABLE_INPUT'):
        # Username rendering must work even if readline supplies no echo.
        import builtins
        builtins.input = lambda prompt: (_ for _ in ()).throw(AssertionError('Username still uses input/readline'))
    signal.signal(signal.SIGALRM, cli.idle)
    try:
        if mode == 'timeout':
            signal.alarm(1)
        result = cli._read_visible('Username: ') if mode == 'visible' else cli._read_masked('Password: ')
        outcome = {'digest': hashlib.sha256(result.encode()).hexdigest()}
    except (EOFError, KeyboardInterrupt, cli.IdleTimeout) as exc:
        outcome = {'exception': type(exc).__name__}
    finally:
        signal.alarm(0)
    outcome['restored'] = termios.tcgetattr(fd) == initial
    print('\r\nRESULT:' + json.dumps(outcome), flush=True)
    raise SystemExit

checks = []

def run_case(name, mode, payload, expected, feedback=None):
    master, slave = pty.openpty()
    def session():
        os.setsid()
        if os.environ.get('CLI_NO_CONTROLLING_TTY') != '1':
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    child = subprocess.Popen([sys.executable, __file__, '--child', mode],
                             stdin=slave, stdout=slave, stderr=slave,
                             preexec_fn=session, env=dict(os.environ, TERM=os.environ.get('CLI_TEST_TERM', 'linux')))
    os.close(slave)
    buffer = b''
    def expect(marker, timeout=6):
        nonlocal buffer
        until = time.monotonic() + timeout
        while marker not in buffer:
            if time.monotonic() > until:
                raise AssertionError(name + ': prompt timeout')
            if select.select([master], [], [], .1)[0]:
                buffer += os.read(master, 65536)
        before, buffer = buffer.split(marker, 1)
        return before
    try:
        expect(b'Username: ' if mode == 'visible' else b'Password: ')
        if payload:
            os.write(master, payload)
        if feedback is not None:
            # Check feedback before Enter, not just the completed input value.
            seen = expect(feedback)
            if mode != 'visible':
                assert b'Synthetic' not in seen
            os.write(master, b'\n')
        before = expect(b'RESULT:')
        result_line = expect(b'\n')
        # The result is the last JSON line after the marker.
        combined = before  # Terminal output must never contain secret input.
        if mode != 'visible':
            assert b'Synthetic' not in combined
        child.wait(timeout=3)
        assert child.returncode == 0
        outcome = json.loads(result_line.strip())
        assert outcome['restored'], name + ': terminal flags not restored'
        if expected in ('EOFError', 'KeyboardInterrupt', 'IdleTimeout'):
            assert outcome.get('exception') == expected, name + ': wrong exception'
        else:
            assert outcome.get('digest') == hashlib.sha256(expected.encode()).hexdigest(), name + ': input changed'
        checks.append({'name': name, 'pass': True})
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        os.close(master)

run_case('username visible with inherited ECHO off', 'visible', b'admin', 'admin', b'admin')
run_case('password stars visible before Enter', 'secret', b'Synthetic!27', 'Synthetic!27', b'*' * 12)
run_case('password Backspace erases a star', 'secret', b'SyntheticX\x7f!27', 'Synthetic!27', b'\b \b')
run_case('password Ctrl-U clears input', 'secret', b'wrong\x15Synthetic!27', 'Synthetic!27', b'\b \b' * 5)
run_case('UTF-8 password masks each character', 'secret', 'Testé!'.encode(), 'Testé!', b'*' * 6)
run_case('arrow keys cannot add escape characters', 'secret', b'Synthetic\x1b[D!27', 'Synthetic!27', b'*' * 12)
run_case('EOF restores terminal flags', 'secret', b'\x04', 'EOFError')
run_case('interrupt restores terminal flags', 'secret', b'\x03', 'KeyboardInterrupt')
run_case('idle expiry restores terminal flags', 'timeout', b'', 'IdleTimeout')

source = os.environ['CLI_INPUT_SOURCE']
result = subprocess.run([sys.executable, '-c',
    'import sys;sys.path.insert(0,sys.argv[1]);import cli;\n'
    'try:cli._read_masked("Password: ")\n'
    'except ValueError:sys.exit(0)\n'
    'sys.exit(1)', source], input='Synthetic!27\n', capture_output=True, text=True)
assert result.returncode == 0 and 'Synthetic' not in result.stdout + result.stderr
checks.append({'name':'redirected password input fails closed', 'pass':True})
print(json.dumps({'passed':len(checks), 'checks':checks}, indent=2))

