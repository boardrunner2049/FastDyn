model RigidBodyFmiProbe
  extends RigidBody.RigidBody6DOF;
equation
  F_b = {0, 0, 0};
  M_b = {0, 0, 0};
end RigidBodyFmiProbe;
