# USP test vectors

Worked examples for the parts of the specification that can be implemented
wrongly without any reviewer noticing. Run them with:

```bash
python tools/usp_check.py vectors
```

CI runs the same command on every push.

## Why these exist

`platform_key_pop` is specified on both bindings and implemented on neither.
Prose review catches a contradictory MUST; it does not catch a canonicalization
rule that two implementers read the same way and encode differently. These
vectors publish the exact bytes, so a second implementation can disagree with
the first *before* both ship.

## Format

One JSON object per file, under `pop/`.

| Field | Meaning |
|---|---|
| `id` | Stable identifier. **Must be cited in `specification.md`** — the checker fails otherwise, so prose and vectors cannot drift apart. |
| `section` | The specification section the vector pins down. |
| `requirement` | Short slug for the rule under test. |
| `binding` | `rest`, `mcp`, or both. |
| `expect` | `accept` or `reject`. |
| `reject_code` | For `reject` vectors. Must appear in the §10.1.1 error-code table **and** the OpenRPC `USPProtocolError` enum. |
| `notes` | What the vector is for, and what a naive implementation gets wrong. |
| `key` | Public JWK, its RFC 7638 thumbprint, and — for reproducibility — the raw private key. |
| `proof` | Compact JWS. Really signed; the checker verifies it. |
| `expected_claims` | The decoded payload, so a mismatch between prose and signature is caught. |
| `credential` | Validated against `schemas/profile.json#/$defs/BookingScopedCredential`. |
| `canonicalization` | `params` → `jcs` bytes → `usp_p` digest, all recomputed by the checker. |

## The keys are deliberately published

`key.private_key_d` is in the repository on purpose. These are test keys derived
from fixed seeds (`0x01…01` and `0x02…02`) so that anyone can regenerate the
vectors byte-for-byte and confirm the signatures independently. **They are not
secret and must never be used for anything.** `K1` is the legitimate platform;
`K2` exists solely to be rejected by `006-cross-key-replay`.

## What the checker verifies

- Every published JCS serialization and `usp_p` digest is recomputed. Non-integer
  JSON numbers are refused outright, which turns the specification's
  RECOMMENDATION about them into something a vector author cannot violate.
- Every signature is verified against the JWK in its own proof header, and every
  published thumbprint is recomputed from that JWK.
- `expected_claims` matches the signed payload.
- A `pop_key_mismatch` vector actually uses a key differing from the credential's
  `cnf.jkt` — otherwise it would pass while testing nothing.
- Every `reject_code` is reachable from both error-code mirrors.
- Every `id` is cited in `specification.md`.

## Adding a vector

Add the file, cite its `id` in `specification.md`, and run the checker. If it is
a rejection case, prefer one that a *plausible* implementation would accept —
`006` is the model: it is a genuinely valid signature that must still be
rejected. A rejection vector that fails for an obvious reason tests the parser,
not the protocol.
