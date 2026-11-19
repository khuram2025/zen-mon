# ZenPlus 1.25.8

Updates no longer need internet access to the Python package index. Some
appliances failed to update from 1.24.x to 1.25.x at step `pip_install` with
"No matching distribution found for pyvmomi==8.0.3.0.1": 1.25 added the
pyvmomi and pypsrp dependencies, and the update step downloaded them from the
public index, which those networks block or reset.

The update package now bundles wheels for every requirement that is new or
re-pinned since its minimum upgrade base (1.24.36), with their dependency
closure, for the supported appliance Python versions (3.10 on Ubuntu 22.04,
3.12 on Ubuntu 24.04). The package's requirements file points pip at those
wheels, so appliances still running an older updater install them when the
index is unreachable. The updated `pip_install` step installs from the
bundled wheels without contacting the index first and only falls back to the
index if they are incomplete. This adds about 13 MB to the package.

textfsm (installed with netmiko) is pinned to 2.1.0, the version already on
appliances: 2.1.1 installs a stray top-level `tests` package.

No migrations and no change to monitoring, alerting or configuration.
Appliance-only package; preserve the signed Windows agent catalog and minimum
upgrade base 1.24.36.

Validation: a 1.24.47 Python environment with the package index resetting
connections reproduced the field failure with the previous package, then
installed the 1.25.8 requirements through both the 1.24.47 updater command
and the new offline-first step. Unit tests cover requirement selection, the
build wiring and the updater fallback; full server suite.
