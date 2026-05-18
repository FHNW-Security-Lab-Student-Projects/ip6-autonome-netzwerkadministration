# ContainerLab Commands

## Basic Lifecycle

```bash
# Deploy / start the lab
sudo containerlab deploy -t testlab.clab.yml

# Destroy / stop the lab (removes containers and management network)
sudo containerlab destroy -t testlab.clab.yml

# Destroy but keep the management network bridge
sudo containerlab destroy -t testlab.clab.yml --keep-mgmt-net

# Redeploy (destroy + deploy in one step)
sudo containerlab redeploy -t testlab.clab.yml
```

After `destroy --keep-mgmt-net`, start the lab again with a normal deploy:

```bash
sudo containerlab deploy -t testlab.clab.yml
```

## Inspection

```bash
# Show status of nodes in a specific lab
sudo containerlab inspect -t testlab.clab.yml

# Show all running labs (no topology file needed)
sudo containerlab inspect --all
```

## Notes

- All commands should be run from the repo root (`/Users/dominikkrebs/IP6/repos/ip6/`) or provide the full path after `-t`.
- `sudo` is required because ContainerLab manages Linux network namespaces and bridges.
- The runtime directory `clab-testlab/` is created on deploy and removed on destroy (gitignored).