# NCM change detection and re-encrypted secrets

A configuration change alert ("Running configuration changed") is raised when the
normalized text of a new backup differs from the previous one
(`app.services.ncm_content.normalize_config`). Normalization removes runtime noise
that changes without anyone changing the configuration.

## FortiOS re-encrypts secrets on every backup (1.25.3)

FortiOS prints every reversible secret as `set <name> ENC <ciphertext>` with a fresh
random salt each time the configuration is shown (IPsec pre-shared keys, local
user passwords, RADIUS/LDAP secrets, SNMP communities, encrypted private keys). Two
back-to-back backups of an unchanged FortiGate therefore differed on hundreds of
lines. In the field four FortiGates raised a change alert on every scheduled backup:
321-590 lines "added" and "removed", all of them `ENC` lines, with an empty redacted
diff ("Sensitive configuration changed").

Comparison now replaces those ciphertexts with a fixed marker. What is still
detected:

* any change outside the encrypted values, including adding or removing a secret
  setting (the `set <name>` line remains);
* FortiOS administrator password hashes (`config system admin` → `set password ENC
  SH2…`), which print identically every time;
* certificates and unencrypted keys; every other platform's configuration is
  unaffected.

A change to only the value of a re-encrypted secret cannot be seen in the text of a
FortiOS backup; FortiOS reports such edits in its own configuration-change log,
which NCM event-triggered backups listen to.

After the upgrade a one-time background pass (on the NCM one-minute timer,
`app.services.ncm_normalization`) recomputes the stored comparison hashes, so
baseline drift is correct again, and closes open change alerts whose two compared
backups are still archived and are identical under the new comparison. Those
closures are recorded as administrative with the reason on the incident timeline;
nothing is sent. Older alerts whose backups were already pruned cannot be verified
and are left for an operator to close.
