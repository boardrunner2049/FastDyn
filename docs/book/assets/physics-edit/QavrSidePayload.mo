within FastDyn;

model QavrSidePayload "QAV-R with a downward load at its front-right motor"
  import Vehicles;
  import Geodesy;
  parameter Real payload_mass = 0.05
    "Payload mass used to prescribe its weight; payload inertia is omitted [kg]";
  // Independent coordinates stay writable as FMI parameters. These equal
  // {0.11 / sqrt(2), -0.11 / sqrt(2), 0} for the assumed QAV-R motor layout.
  parameter Real attachment_b[3] = {0.07778174593052023, -0.07778174593052023, 0}
    "Front-right motor (motor 1), from CG in body FLU [m]";

  extends Qavr(plant(external_force_b = force_b, external_moment_b = moment_b));

  Real force_world[3] "Prescribed payload weight in world NWU [N]";
  Real force_b[3] "Applied force expressed in body FLU [N]";
  Real moment_b[3] "Moment about vehicle CG in body FLU [N*m]";
equation
  // The load stays world-down when the aircraft tilts.
  force_world = {0, 0, -payload_mass * plant.gravity};
  force_b = transpose(plant.R) * force_world;
  moment_b = cross(attachment_b, force_b);
  // This is an external load, not an additional rigid body's inertial dynamics.
end QavrSidePayload;
