# CI/tests_v2 cluster test campaign

## Baseline

The first complete KinD run used the local `ci-krkn` cluster kubeconfig (`/tmp/krkn-kind-kubeconfig`) and the repository venv:

```bash
KUBECONFIG=/tmp/krkn-kind-kubeconfig ./venv/bin/python -m pytest CI/tests_v2/ -v --timeout=300 --reruns=2 --reruns-delay=10 --html=CI/tests_v2/report-kind-baseline.html --junitxml=CI/tests_v2/results-kind-baseline.xml
```

Result: **54 collected, 53 passed, 1 skipped**, exit 0, 1464.20 seconds. The skip is the two-worker `instance_count` network test; this KinD cluster has one schedulable worker.

The first OpenShift attempt did not provide `HOME` to the Kraken subprocess. The pytest fixtures used the supplied kubeconfig, but child Kraken processes resolved the workstation default kubeconfig and ran against KinD. That run is retained as an invalid diagnostic and is not used as OpenShift evidence.

## Corrected OpenShift baseline

The corrected command supplied both the explicit kubeconfig and an isolated HOME containing the same OpenShift kubeconfig:

```bash
HOME=/tmp/krkn-ocp-home KUBECONFIG=/Users/darjain/projects/krkn-chaos/krkn/example/pre-chaos-check-test/kubeconfig ./venv/bin/python -m pytest CI/tests_v2/ -v --timeout=300 --reruns=1 --reruns-delay=5 -n auto --dist loadgroup --html=CI/tests_v2/report-openshift-final.html --junitxml=CI/tests_v2/results-openshift-final.xml -o junit_logging=all -rA
```

Result after platform scoping: **54 collected, 46 passed, 8 skipped**, exit 0, 769.20 seconds. The skips are only contracts requiring local KinD/Minikube node containers: Docker-backed node lifecycle, local `tc` inspection, and container-disruption assertions whose observed effect is not available on the remote CRI-O cluster.

Artifacts:

- `CI/tests_v2/results-kind-baseline.xml`
- `CI/tests_v2/report-kind-baseline.html`
- `CI/tests_v2/results-openshift-final.xml`
- `CI/tests_v2/report-openshift-final.html`

## Fixes verified individually

Targeted scenario runs passed on both clusters after the changes:

| Scenario | KinD | OpenShift |
|---|---:|---:|
| pod disruption | 1 passed | 1 passed |
| pod error scenarios | 6 passed | 6 passed |
| namespace deletion | 8 passed | 8 passed |
| storage throttle | 5 passed | 5 passed |
| container scenarios | 4 passed | positive disruption tests are intentionally platform-scoped on OpenShift |

Changes were limited to arbitrary-UID-compatible workload fixtures, a Bash-capable storage target image, and explicit `kind_only` marker handling. No tests were made pass by weakening assertions or converting failures to unconditional skips.
