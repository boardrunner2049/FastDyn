within FastDyn;

model Rover
  RigidBody.Examples.RoverPlant plant;

  parameter Real pwm_min = 1000.0 "Minimum PWM";
  parameter Real pwm_trim = 1500.0 "Neutral PWM";
  parameter Real pwm_max = 2000.0 "Maximum PWM";
  parameter Real lat0 = 40.414929 "Reference latitude [deg]";
  parameter Real lon0 = -86.932387 "Reference longitude [deg]";
  parameter Real ground_alt_wgs84 = 149.0 "WGS84 ellipsoid altitude of the local ground plane [m]";
  parameter Real gps_bias[3] = {0, 0, 0} "GPS bias N/E/altitude [m]";
  parameter Real baro_alt_bias = 0.0 "Barometer relative altitude bias [m]";
  parameter Real earth_radius_m = 6378137.0 "Spherical Earth radius used for local geodetic conversion [m]";
  parameter Real pi = 3.141592653589793;

  input Real pwm[4](start = {1500, 1500, 1500, 1500}) "Servo PWM commands";

  output Real pos_ned[3](each unit="m") "Position N/E/D relative to the local ground origin";
  output Real pos_gps[3] "GPS latitude [deg], longitude [deg], WGS84 ellipsoid altitude [m]";
  output Real vel_ned[3](each unit="m/s") "World velocity N/E/D";
  output Real vel_frd[3](each unit="m/s") "Body velocity F/R/D";
  output Real accel_ned[3](each unit="m/s2") "World kinematic acceleration N/E/D";
  output Real accel_frd[3](each unit="m/s2") "Body kinematic acceleration F/R/D";

  output Real rpy[3](each unit="rad") "ZYX attitude of FRD body in NED, ordered roll/pitch/yaw";
  output Real rotation[3, 3] "Rotation matrix from body FRD to world NED";
  output Real yaw_deg(unit="deg") "Yaw attitude of FRD body in NED";
  output Real omega_frd[3](each unit="rad/s") "Angular velocity about FRD body axes";
  output Real alpha_frd[3](each unit="rad/s2") "Angular acceleration about FRD body axes";

  output Real mag[3](each unit="Gauss") "Body FRD magnetometer";
  output Real baro_altitude_m(unit="m") "Barometer relative altitude";
  output Real baro_pressure_pa(unit="Pa") "Barometer pressure";
  output Real baro_temperature_c(unit="degC") "Barometer temperature";
  output Real baro_climb_rate_mps(unit="m/s") "Barometer climb rate";
  output Real motor_cmd[4] "Normalized steering, normalized throttle, forward speed [m/s], FRD yaw rate [rad/s]";

protected
  Real steering;
  Real throttle;
  Real gps_lat_lon[2];
  Real angular_momentum_flu[3];
  Real net_torque_flu[3];

equation
  steering = min(1.0, max(-1.0, (pwm[1] - pwm_trim) / (pwm_max - pwm_trim)));
  throttle = min(1.0, max(-1.0, (pwm[3] - pwm_trim) / (pwm_max - pwm_trim)));
  plant.steering = steering;
  plant.throttle = throttle;

  mag = plant.mag;

  gps_lat_lon = Geodesy.localNorthEastToLatLon(
    lat0,
    lon0,
    plant.p[1] + gps_bias[1],
    -plant.p[2] + gps_bias[2],
    earth_radius_m);
  pos_gps[1] = gps_lat_lon[1];
  pos_gps[2] = gps_lat_lon[2];
  pos_gps[3] = ground_alt_wgs84 + plant.p[3] + gps_bias[3];

  vel_ned[1] = plant.v_w[1];
  vel_ned[2] = -plant.v_w[2];
  vel_ned[3] = -plant.v_w[3];

  vel_frd = plant.R_FRD_FLU * plant.v_b;
  rotation = transpose(plant.R_NWU_NED) * plant.R * transpose(plant.R_FRD_FLU);
  rpy[1] = atan2(rotation[3,2], rotation[3,3]);
  rpy[2] = asin(min(1.0, max(-1.0, -rotation[3,1])));
  rpy[3] = atan2(rotation[2,1], rotation[1,1]);
  yaw_deg = rpy[3] * 180.0 / pi;

  omega_frd = plant.gyro;
  angular_momentum_flu = plant.J * plant.omega;
  net_torque_flu = plant.M_b - cross(plant.omega, angular_momentum_flu);
  alpha_frd = angularAccelerationFRD(plant.J, net_torque_flu);
  pos_ned = {plant.p[1], -plant.p[2], -plant.p[3]};
  // F_b excludes gravity. Rotate it into world axes, divide by mass and restore gravity.
  accel_ned = plant.R_NWU_NED * ((plant.R * plant.F_b) / plant.mass + {0, 0, -plant.g});
  accel_frd = transpose(rotation) * accel_ned;

  baro_altitude_m = plant.p[3] + baro_alt_bias;
  baro_temperature_c = 15.0 - 0.0065 * (ground_alt_wgs84 + baro_altitude_m);
  baro_pressure_pa = 101325.0 * (1.0 - 2.25577e-5 * (ground_alt_wgs84 + baro_altitude_m)) ^ 5.25588;
  baro_climb_rate_mps = plant.v_w[3];
  motor_cmd = {steering, throttle, vel_frd[1], omega_frd[3]};
end Rover;
