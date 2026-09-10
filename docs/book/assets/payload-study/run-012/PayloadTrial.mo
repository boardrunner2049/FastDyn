within FastDyn;
model PayloadTrial
  import Vehicles;
  import Geodesy;
  import RigidBody;
  extends QavrSidePayload(
    bare_mass=0.5,
    payload_mass=0.24216166022695873,
    attachment_b={0.07778174593052023, -0.07778174593052023, 0});
end PayloadTrial;
