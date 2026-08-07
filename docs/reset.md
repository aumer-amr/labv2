# Reset

> [!CAUTION]
> Reset is destructive. It removes Kubernetes state and returns Talos nodes to maintenance mode. Verify exact targets and recovery material before continuing.

## Reset All Nodes

```sh
just talos reset
```

## Reset One Node

```sh
just talos reset-node <node>
```

Both recipes require confirmation. Never bypass it.

Repeated full rebuilds can trigger container registry or certificate authority rate limits.

After reset, follow the [deployment runbook](deployment.md) for a clean rebuild.
