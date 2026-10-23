# Optional recovery infrastructure

**A, B and C may ignore every module and interface listed below for Phase 1.**
The supported minimum path is documented in [PHASE1_INTEGRATION.md](../PHASE1_INTEGRATION.md).
It uses factories rather than restore operations. Do not make the advanced APIs
prerequisites for integrating a new workflow.

The existing [detailed design](d_infrastructure.md) describes a separate opt-in path:

| Optional component | Purpose | Extra contract only when opted in |
|---|---|---|
| `RecoveryPointManager` | Bind environment, workflow and artifacts at a quiescent boundary | Snapshot/restore and context export/restore |
| C0–C3 recovery points | Initial and stage-boundary continuation | No in-flight calls; consistent node position and artifact map |
| `FullRestartPolicy` / `RecoveryCoordinator` in `experiments.recovery` | Apply a checkpoint-based recovery plan | Initial recovery point, explicit cache cleanup, compatibility checks |
| Component hashes and configuration fingerprints | Reject corrupted/incompatible recovery points | Stable serialized components |
| Context and cache restoration checks | Avoid continuing with stale histories | B-defined state and cleanup semantics |

These features have primarily mock validation; see the [status table](PHASE1_CAPABILITIES.md).
The checkpoint-based coordinator remains for advanced tests and experimentation.
It is not the default Phase 1 integration entry point.

Phase 2 can build on the existing immutable artifacts, data/state/control dependencies,
attempt history and evaluations. A future selective policy must identify potential
downstream invalidation, select a compatible recovery point, verify retained results
against restored state and rerun affected nodes. Conservatively recorded dependencies
may require more recomputation; they are not evidence that an error was adopted.

Selective recovery also needs state-write lineage, control-path validity, and checks
that environment state and agent contexts agree. If validity cannot be established,
expand recovery or fall back to a full restart. Record unresolved irreversible effects
separately. Restoring simulated state cannot erase historical violations or costs.
The current code does not implement selective policy, graph invalidation or safe reuse.
