within FastDyn;

model Qavr "Original 5-inch QAV-R: 220 mm diagonal; assumed tutorial equipment"
  import Vehicles;
  import Geodesy;
  parameter Real bare_mass = 0.50 "Flying mass including battery [kg]";
  parameter Real bare_inertia[3,3] = diagonal({0.0008973333333333334, 0.00126, 0.002066666666666667})
    "Estimated inertia of the equipped frame [kg*m^2]";

  extends Copter(
    mass = bare_mass, inertia = bare_inertia,
    arm_length = 0.11,
    Ct = 1.6e-6, Cm = 0.009, omega_max = 2300,
    tau_up = 0.015, tau_down = 0.025,
    body_area = 0.01936,
    drag_area = {0.011616, 0.015488, 0.023232},
    linear_drag = {0.023232, 0.023232, 0.034848},
    leg_x = 0.07, leg_y = 0.07, leg_z = -0.03,
    ground_k = 600, ground_c = 30, ground_tangent_c = 5);
end Qavr;
