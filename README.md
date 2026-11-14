# OpenShift physical node maintenance

Run an ordered shutdown or startup of one OpenShift node using `oc` and its Dell iDRAC Redfish API. Run from a management host that stays online throughout the operation.

## Prerequisites

- Python 3 and Ansible Core (for example, `python3 -m pip install --user ansible-core`). No additional Ansible collections are required.
- `oc` on the management host, logged into the intended cluster with permission to read nodes and pods, cordon/uncordon nodes, and evict workloads. Check with `oc whoami` and `oc whoami --show-server`.
- Network access to the iDRAC HTTPS endpoint and an iDRAC account authorized to read system state and control power. The default Redfish system ID is `System.Embedded.1`.
- A trusted iDRAC HTTPS certificate. If your lab uses a self-signed certificate, explicitly set `idrac_validate_certs: false` in the encrypted variables file; this disables TLS certificate verification.
- Confirm independently that the OpenShift node and iDRAC address identify **the same physical server**. The playbook cannot verify that mapping. An incorrect address can power off a different server.

Before shutdown, assess cluster capacity, PodDisruptionBudgets, storage, quorum, and critical workloads. The playbook refuses control-plane nodes unless `allow_control_plane: true` is explicitly supplied. That override does not check quorum or make the operation safe. **Drain always uses `--delete-emptydir-data=true --ignore-daemonsets=true --force=true --timeout=30m`: it can discard `emptyDir` data and evict unmanaged pods.** Review affected workloads before running it. DaemonSet pods are ignored; PodDisruptionBudgets are still respected.

## Configure credentials

Create a local file outside the repository, for example `~/node-maintenance.yml`:

```yaml
node_name: worker-0.example.com
idrac_host: idrac-worker-0.example.com
idrac_username: maintenance-user
idrac_password: replace-me
# idrac_system_id: System.Embedded.1
# idrac_validate_certs: false  # only if you accept unverified TLS
```

Encrypt it immediately with `ansible-vault encrypt ~/node-maintenance.yml`. Protect the vault password separately; never commit credentials or vault password files. Create one encrypted file per node or change the mapping carefully for each run.

## Run

```bash
# Inspect the target cluster and ensure the node/iDRAC mapping is correct.
oc whoami --show-server
oc get node worker-0.example.com

# Cordon, drain, request graceful shutdown, and wait for iDRAC PowerState Off.
ansible-playbook maintenance.yml -e @~/node-maintenance.yml -e action=shutdown --ask-vault-pass

# Power on, wait for iDRAC PowerState On and node Ready, uncordon, and verify.
ansible-playbook maintenance.yml -e @~/node-maintenance.yml -e action=startup --ask-vault-pass
```

The startup output lists resident pods for review. `Ready` and schedulable do not establish workload health: inspect those pods, cluster operators (`oc get clusteroperators`), alerts, and application-specific health before declaring the node fully recovered. Power-on and Ready timeouts default to 10 and 20 minutes respectively; adjust `power_wait_retries`, `power_wait_delay`, `node_wait_retries`, or `node_wait_delay` in the encrypted variables file if needed.

## Failure handling

The playbook stops on drain failure without sending a power-off request. It leaves the node cordoned; inspect the error and workloads before retrying or manually running `oc adm uncordon NODE`. A graceful shutdown that times out is **not** forced; inspect iDRAC and the host. If startup fails before uncordon, the node remains cordoned if it was cordoned beforehand. If final verification fails after uncordon, inspect the node immediately and cordon it again if needed. A request may have reached iDRAC even if its response times out: check the actual power state before retrying.

The playbook uses Redfish Basic authentication over HTTPS and expects a Dell iDRAC implementing the standard ComputerSystem reset action. It has no automated check of hardware identity, workload recovery, control-plane quorum, or cluster-wide health. Test in a non-production environment first.

## Lab shutdown: Luke control02

On 2026-11-13, `control02.syangsao.net` was shut down on the Luke cluster. It is both a control-plane and worker node in a two-control-plane-plus-arbiter topology. This is an operational record, not a guarantee that the same procedure is safe on another day.

1. Selected the Luke kubeconfig and checked the API server, nodes, cluster operators, disruption budgets, and pods on the target. Both control-plane nodes and the arbiter were Ready; all three etcd pods were running. An NFS CSI controller pod on the target was already in `CrashLoopBackOff`.
2. Compared the OpenShift node's system UUID with the Redfish `ComputerSystem` UUID. They matched. The iDRAC certificate was self-signed and not trusted; the operator explicitly approved unverified TLS for this run. A UUID obtained over unverified TLS does not authenticate the endpoint. Prefer installing a trusted certificate.
3. Ran the shutdown playbook with the control-plane override. It cordoned the node but stopped at drain because pods used `emptyDir` storage. No power-off request was sent.
4. Retried drain with `--delete-emptydir-data=true --ignore-daemonsets=true`. It stopped because four control-plane guard pods had no declared controller. The node remained cordoned.
5. After explicit approval to delete unmanaged pods, ran the following command. Drain respected PodDisruptionBudgets and eventually completed:

   ```bash
   KUBECONFIG="$HOME/ocp422-luke/auth/kubeconfig" \
     oc adm drain control02.syangsao.net \
     --delete-emptydir-data=true --ignore-daemonsets=true \
     --force=true --timeout=30m
   ```

   `--delete-emptydir-data` discards data in affected `emptyDir` volumes. `--force` permits eviction of unmanaged pods; it does not bypass PodDisruptionBudgets. Review affected workloads and obtain approval for both risks before using these flags.
6. After successful drain, separately verified the iDRAC-reported UUID again, sent the Redfish `GracefulShutdown` action, and observed `PowerState: Off`. This separate power request was not performed by the playbook. The iDRAC connection used unverified TLS as approved above.
7. Immediately after power-off, the arbiter and `control01` remained Ready. OpenShift still displayed `control02` as `Ready,SchedulingDisabled`; node status can lag the physical power state. Losing another etcd member while `control02` is off would lose quorum.

The playbook now uses the same drain flags by default. This change was made after the recorded shutdown; that shutdown used a manual drain and separate power request. Do not skip identity and health checks, or power off after a failed drain.

To bring the node back, use the startup playbook command above with the Luke kubeconfig and the node's encrypted variables file. If the iDRAC certificate remains untrusted, the documented explicit TLS exception is still required. Then verify the node, resident pods, cluster operators, etcd and application health; startup has not yet been tested against this node.
