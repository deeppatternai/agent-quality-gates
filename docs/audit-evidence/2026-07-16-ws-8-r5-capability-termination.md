# WS-8 R5 capability termination receipt

- Study: `ws-8-aqg-cross-host-2026-07-r5`
- Phase: `capability`
- State: terminal and immutable
- Resumable: `false`
- Completed capability cells: `0 / 2`
- Capability receipts: absent
- Accounted model calls: `0`
- Accounted spend: `0 USD`
- Aggregate new WS spend: `0 USD` of the Owner-authorized `200 USD` ceiling
- Usage evidence incomplete: `false`
- Safe boundary: the capability attempt terminated before an accounted model call.

R5 cannot be resumed, retried, supplied with a replacement nonce, or used as a
capability prerequisite. Pilot, primary, and stress were not executed. Any
successor requires a distinct Owner-authorized study after root-cause work; it
must preserve this terminal record as immutable evidence.

No nonce, envelope, terminal JSON, raw stream, credential, Keychain value,
account-proof content, protected local path, or private failure detail appears
in this receipt.
