within FastDyn;

function angularAccelerationFRD
  "Solve Euler's rigid-body equation in FLU and express the acceleration in FRD"
  input Real J[3,3] "Symmetric body inertia tensor";
  input Real torque[3] "FLU moment minus omega cross angular momentum";
  output Real alpha[3] "FRD angular acceleration";
protected
  Real c11, c22, c33, c12, c13, c23 "Symmetric cofactor matrix of J";
  Real determinant;
algorithm
  c11 := J[2,2]*J[3,3] - J[2,3]*J[2,3];
  c22 := J[1,1]*J[3,3] - J[1,3]*J[1,3];
  c33 := J[1,1]*J[2,2] - J[1,2]*J[1,2];
  c12 := J[1,3]*J[2,3] - J[1,2]*J[3,3];
  c13 := J[1,2]*J[2,3] - J[1,3]*J[2,2];
  c23 := J[1,2]*J[1,3] - J[1,1]*J[2,3];
  determinant := J[1,1]*c11 + J[1,2]*c12 + J[1,3]*c13;
  alpha[1] :=  (c11*torque[1] + c12*torque[2] + c13*torque[3]) / determinant;
  alpha[2] := -(c12*torque[1] + c22*torque[2] + c23*torque[3]) / determinant;
  alpha[3] := -(c13*torque[1] + c23*torque[2] + c33*torque[3]) / determinant;
end angularAccelerationFRD;
