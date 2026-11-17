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

Run one node at a time. These examples use the Luke cluster and `control02.syangsao.net`; use a different kubeconfig and encrypted variables file for another cluster or node.

1. Select the cluster and review its health and workloads:

   ```bash
   export KUBECONFIG="$HOME/ocp422-luke/auth/kubeconfig"
   oc whoami --show-server
   oc get nodes
   oc get clusteroperators
   oc get pdb -A
   oc get pods -A --field-selector spec.nodeName=control02.syangsao.net -o wide
   ```

   Verify the node-to-iDRAC mapping independently, check etcd quorum and cluster capacity, and review affected `emptyDir` data and unmanaged pods. Do not proceed if the disruption is unsafe.

2. Shut down the node using its encrypted variables file:

   ```bash
   ansible-playbook maintenance.yml -e @~/node-maintenance.yml \
     -e action=shutdown -e allow_control_plane=true --ask-vault-pass
   ```

   Omit `-e allow_control_plane=true` for workers. For control-plane nodes, this explicit override is required but does not verify quorum. The playbook cordons the configured node and runs `oc adm drain` with `--delete-emptydir-data=true --ignore-daemonsets=true --force=true --timeout=30m`. It requests graceful power-off only if drain succeeds, then waits for iDRAC to report `Off`.

3. To return the node to service, select the same kubeconfig and run:

   ```bash
   ansible-playbook maintenance.yml -e @~/node-maintenance.yml \
     -e action=startup --ask-vault-pass
   ```

   The playbook requests power-on, waits for iDRAC `On` and OpenShift `Ready`, uncordons the node, verifies it is Ready and schedulable, and lists resident pods.

4. Check node, workloads, operators, etcd, alerts and applications before declaring recovery:

   ```bash
   oc get nodes
   oc get pods -A --field-selector spec.nodeName=control02.syangsao.net -o wide
   oc get pods -n openshift-etcd -o wide
   oc get clusteroperators
   ```

`Ready` and schedulable do not establish workload health. Power-state and node Ready timeouts default to 10 and 20 minutes respectively; adjust `power_wait_retries`, `power_wait_delay`, `node_wait_retries`, or `node_wait_delay` in the encrypted variables file if needed. If using an untrusted self-signed iDRAC certificate, `idrac_validate_certs: false` must be explicitly configured; this disables server identity verification for both startup and shutdown.

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

## Lab startup: Luke control02

On 2026-11-16, `control02.syangsao.net` was powered back on with the startup playbook and returned to service. This is an operational record.

1. Pre-flight: the node was `NotReady,SchedulingDisabled`; `control01` and the arbiter were Ready and all three etcd pods were running, so quorum held while control02 was off. iDRAC reported `PowerState: Off`.
2. The first run failed at "Read iDRAC system" with `SSL: CERTIFICATE_VERIFY_FAILED: self-signed certificate`: the encrypted variables file was missing `idrac_validate_certs: false`, so the playbook default (`true`) applied. This is a configuration gap, not a playbook bug — the README already requires the explicit TLS exception for this iDRAC. The variable was added to the encrypted file and it was re-encrypted; no playbook code changed.
3. The second run succeeded in about 4.5 minutes: power-on request, iDRAC `On` wait, node Ready wait, uncordon, and final verification all passed (`ok=10 changed=1 failed=0`).
4. Post-startup the cluster was still settling, as expected: kube-apiserver and openshift-controller-manager were progressing with control-plane reinstall/replica reconciliation, and monitoring remained degraded from before this run (Prometheus operator webhook rollout stuck since 2026-11-14). All etcd members were healthy.

Startup has now been tested against this node end to end.

## Scheduled weekly maintenance

`node-maintenance.py` runs the playbook for one node and emails the result (success or failure, with the last 60 lines of playbook output). It is driven by cron on the management host:

| Day | Time | Action | Node |
|---|---|---|---|
| Mon | 07:00 | startup | control02 |
| Mon | 18:00 | shutdown | control01 |
| Tue | 07:00 | startup | control01 |
| Tue | 18:00 | shutdown | control02 |
| Wed | 07:00 | startup | control02 |
| Wed | 18:00 | shutdown | control01 |
| Thu | 07:00 | startup | control01 |
| Thu | 18:00 | shutdown | control02 |
| Fri | 07:00 | startup | control02 |
| Fri | 18:00 | shutdown | control02 |

The script expects:

- `KUBECONFIG` pointing at the Luke cluster (`~/ocp422-luke/auth/kubeconfig` is set internally).
- A vault password file at `/tmp/.nm-vaultpass` (mode 600) so cron can run unattended. If it is missing, the script emails an error instead of running the playbook. `/tmp` may be cleared on reboot; restore the file or the next scheduled run will fail with a notification.
- An SMTP config at `node-maintenance.smtp.conf` (mode 600, gitignored) in INI format:

  ```ini
  [smtp]
  host = smtp.gmail.com
  port = 587
  secure = tls
  username = you@gmail.com
  password = <app password>
  from = you@gmail.com
  to = recipient@example.com
  ```

  Gmail requires an app password (Google Account → Security → 2-Step Verification → App passwords); SMTP AUTH is disabled for Microsoft 365 mailboxes, so Outlook cannot be used as the relay.

Logs: `logs/node-maintenance.log` (script) and `logs/cron.log` (cron stdout/stderr), both gitignored. Exit codes: 0 playbook succeeded and email sent, 1 playbook failed (email sent), 2 email failed after the playbook finished.

Operational notes: shutdowns are real — the playbook cordons, drains with the documented destructive flags, then sends `GracefulShutdown` to iDRAC. The Friday 18:00 → Monday 07:00 window leaves control02 off for about 61 hours; quorum holds on the remaining two etcd members, but a second control-plane failure in that window loses quorum.
