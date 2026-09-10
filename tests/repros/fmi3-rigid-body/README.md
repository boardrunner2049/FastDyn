# Rigid-body FMI regression probe

The pinned Rumoca compiler now exports this rigid-body model while retaining
its physical-parameter assertion. From the repository root, in the selected
development environment:

```bash
rumoca compile tests/repros/fmi3-rigid-body/RigidBodyFmiProbe.mo \
  --model RigidBodyFmiProbe \
  --source-root third_party/common/modelica_models \
  --target fmi3 --output out/repros/rigid-body
```

Expect `out/repros/rigid-body/RigidBodyFmiProbe.fmu`. The supported vehicle and
payload checks retain the original library validation equations.

The remaining Plane limitation involves continuous ground-contact events.
`tests/integration/plane_export_limit_test.py` checks that actual model's DAE
lowering and the compiler's explicit export refusal. It is separate from this
resolved rigid-body assertion case.
