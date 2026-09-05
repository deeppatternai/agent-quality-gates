# WS-8 R4 capability termination receipt

- Study: `ws-8-aqg-cross-host-2026-07-r4`
- State: terminal and immutable
- Completed capability cells: `0 / 2`
- Accounted model calls: `0`
- Accounted spend: `0 USD`
- Usage evidence incomplete: `false`
- Failure boundary: source snapshot canonical-path validation, before either model subprocess
- Root cause: macOS canonicalizes `/var/tmp` beneath `/private/var/tmp`, while the strict R4 guard required the supplied snapshot path to equal its resolved path
- Successor rule: use a fresh R5 study whose fixed execution root is already canonical

R4 cannot be resumed, retried, supplied with a replacement nonce, or used as a
capability prerequisite. Its attempt and terminal state remain preserved.

No nonce, envelope, terminal JSON, raw stream, credential, Keychain value,
account-proof content, or protected local path appears in this receipt.
