within FastDyn;

model QavrVaryingLoad "QAV-R with a smoothly varying tension at the front-right motor"
  import Vehicles;
  import Geodesy;
  parameter Real payload_mass = 0.05 "Reference mass for the mean applied weight [kg]";
  parameter Real modulation = 0.5 "Fractional variation about the mean, between 0 and 1";
  parameter Real period = 5.0 "Load period [s], greater than zero";
  // Independent coordinates, matching 0.11 / sqrt(2) at the QAV-R motor.
  parameter Real attachment_b[3] = {0.07778174593052023, -0.07778174593052023, 0}
    "Front-right motor, from CG in body FLU [m]";

  extends Qavr(plant(external_force_b = force_b, external_moment_b = moment_b));

  Real force_world[3] "Prescribed tension in world NWU [N]";
  Real force_b[3] "Applied force in body FLU [N]";
  Real moment_b[3] "Moment about the vehicle CG in body FLU [N*m]";
equation
  assert(period > 0 and modulation >= 0 and modulation <= 1,
    "Use a positive period and modulation between zero and one");
  // Compared with QavrSidePayload, the force law now varies continuously in time.
  force_world = {0, 0, -payload_mass * plant.gravity *
    (1 + modulation * sin(2 * pi * time / period))};
  force_b = transpose(plant.R) * force_world;
  moment_b = cross(attachment_b, force_b);
end QavrVaryingLoad;
