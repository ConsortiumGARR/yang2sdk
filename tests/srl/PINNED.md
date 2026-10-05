# Pinned lab versions (LAB ONLY)

Everything the SR Linux lab's behaviour depends on, pinned in one place so a
red pipeline can be traced to a version bump rather than to mystery.

`tests/srl/srl.clab.yml` and `.github/workflows/ci-srl.yaml` are the two files
that consume these. **Update the tag and the digest together, in the same
commit.** A tag alone can be re-pointed upstream; a digest cannot.

## Container image

| | |
| --- | --- |
| Image | `ghcr.io/nokia/srlinux:25.10.1` |
| Manifest-list digest | `sha256:bc8112667b5a87bee5039ade65b504ac2ef35511210d0675db6c7b0754e8cc4c` |
| `linux/amd64` digest | `sha256:156ee7cee6c7159e808e8f0f7bdd15ba5953d1faf7198532d477516ea4711400` |
| `linux/arm64` digest | `sha256:cc17674a2de09eacaf57b101eb60b837d1e129c7cfbc05d44bb65be6b0ecda72` |
| Chassis type | `ixr-d2l` |
| Approx. compressed size | 750 MB (~2 GB on disk) |

Re-read the digests with:

```bash
docker buildx imagetools inspect ghcr.io/nokia/srlinux:25.10.1
```

The image is free and requires no registration or licence key. Unlicensed, the
datapath is capped at 1000 PPS and `sr_linux` restarts itself weekly — neither
matters for an ephemeral CI container, and neither is a reason to add a licence.

## Containerlab

| | |
| --- | --- |
| Version | `0.79.0` |
| Pinned as | `CLAB_VERSION` in `.github/workflows/ci-srl.yaml` |

Containerlab is a build-time dependency here: a new release can change deploy or
readiness behaviour with no change to this repository, which is exactly the kind
of failure that erodes trust in a CI gate. Bump it deliberately and say so in the
PR. Upstream release notes: <https://containerlab.dev/rn/>

## YANG models

The CI job does **not** use a pinned model tarball. It pulls the node's own
library over `get-schema` (RFC 6241 §7 / `ietf-netconf-monitoring`) and compiles
from that, which is the point: the SDK is generated from the models the device
actually runs, not from a repository copy that can drift from the image.

The offline `nokia/srlinux-yang-models` repository is the *manual* fallback for
working without a live node. Its `v25.10.1` tag matches the image tag above; it
is intentionally not used by CI.

## Host requirement worth knowing

SR Linux's DPDK-based XDP datapath needs the **SSSE3** instruction set, and
containerlab *aborts* deployment without it. GitHub's `ubuntu-24.04` runners
(AMD EPYC) have it. Emulated CPU models under QEMU/Proxmox often do not — if the
lab will not deploy locally, check this before anything else.