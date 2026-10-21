# Security Policy

## Scope

`yang2sdk` is a **generator**. It reads YANG modules from a device and emits a
Python package. It does not hold credentials, ship device configuration, or
run anything against production gear. The things worth reporting are therefore
about the generator and the code it emits.

In scope:

- the generator (`src/yang2sdk/`), its CLI entry points, and its Jinja templates;
- the lab tooling (`yang-downloader`, `sdk-verify`);
- generated SDKs — specifically anything that would leak or mishandle a
  credential, or weaken a transport default.

Out of scope:

- **vulnerabilities in a device's own management stack.** If SR Linux, a `notconf`
  image, or a real device is exposed through the generated client, report it to
  that vendor.
- configuration errors you make against a lab device.

## Reporting

Report privately to the maintainers via
[GitHub's security advisory form](https://github.com/ConsortiumGARR/yang2sdk/security/advisories/new).
Please do not open a public issue for a suspected vulnerability.

Include the generator version (`pyproject.toml`), the YANG module set if
relevant, and a minimal reproduction. Note whether the generated client was
produced by a released version or from `main`.

## Credentials

`.env` holds lab credentials and is gitignored; it is never committed. If you
accidentally commit a credential, rotate it — do not rely on history rewriting
alone, since a pushed secret must be assumed disclosed.

Generated packages must never embed credentials. The generated clients read
`DEVICE_USER` plus `DEVICE_PASS` (password) or `DEVICE_SSH_KEY` /
`DEVICE_SSH_KEY_PASSPHRASE` (NETCONF public-key authentication) from the
environment at runtime, or take the equivalent constructor arguments, and
nothing else. Credentials are resolved by source: an explicit password
suppresses the environment key pair; an explicit key suppresses `DEVICE_PASS`
and only accepts a passphrase from the explicit argument; both explicit values
are rejected as ambiguous by the generated clients and downloader
(`sdk-verify` warns and uses the key for NETCONF to keep `--protocol both`
usable). With neither explicit, the environment is used, and
if both `DEVICE_PASS` and `DEVICE_SSH_KEY` are set the key wins with a logged
warning. That is enforced by
`test_both_protocols_fail_closed_without_credentials` and the key-auth tests in
`tests/test_matrix.py`: a client with neither a password nor a key raises
`ValueError` before any transport work. Key paths are validated for the same
reason, and an explicit key disables the SSH agent and `~/.ssh` lookups — a
nonexistent or unauthorized explicit key must not silently authenticate as a
different identity. A certificate path also requires its adjacent private key.
The **account password** is never placed in ncclient's key-passphrase slot
(paramiko rejects an unencrypted key when a password is supplied, which would
silently downgrade to password auth). ncclient will, however, try an encrypted
key's passphrase as a password if public-key authentication fails
(`SSHSession._auth`); the client logs a warning naming that fallback. Use an
unencrypted key if that behaviour is unacceptable.
`DEVICE_USERNAME` / `DEVICE_PASSWORD` are **retired and must stay inert** — see
`AGENTS.md` for why re-adding them as an alias was tried and reverted.

## Transport defaults

These are security controls, not tuning knobs. Changing one requires explicit
justification in the PR description:

| Default | Value | Enforced by |
| --- | --- | --- |
| RESTCONF TLS verification | `verify=True` | `verify=False` only as an explicit opt-out, with a logged warning |
| NETCONF host-key verification | `verify=True` | `hostkey_verify` is a deprecated alias only; same opt-out rule |
| NETCONF auto-commit | `auto_commit=False` | opt-in only; the safe sequence is edit → validate → commit (RFC 6241 §8.6.4.1, §8.3.4.1) |
| `replace()` completeness | refuses models with unset fields | `allow_partial=True` is an explicit, deliberate override |

A PR that flips one of these without naming the security tradeoff will be
rejected.

## Lab gear

`sdk-verify` and `yang-downloader` are **lab only**. The `crud` tier writes to
the device. Never point them at production: an unbounded read of `restconf/data/`
can drive a real device to 100% CPU and trigger a watchdog reboot or OOM kill.