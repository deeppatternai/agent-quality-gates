# WS-8 R3 capability termination

- Study: `ws-8-aqg-cross-host-2026-07-r3`
- Phase: `capability`
- State: terminal and immutable
- Resumable: `false`
- Completed cells: `0`
- Expected cells: `2`
- Accounted model calls: `0`
- Accounted spend: `0 USD`
- Usage incomplete: `false`
- Redaction-safe reason: the live executor referenced an undefined account value before constructing a model subprocess

R3 cannot be resumed, retried, supplied with a replacement nonce, or used with
another envelope. R4 is a distinct Owner-approved study with a new protocol,
source binding, account-proof schema, envelope, and nonce.

No nonce, envelope, terminal JSON, raw stream, credential, Keychain value,
account-proof content, or protected local path is present in this record.
