within FastDyn;

model Copter
  parameter Real mass = 2.5644001 "Gazebo gs_drone equivalent mass [kg]";
  parameter Real ixx = 0.02601237985 "Gazebo gs_drone equivalent inertia xx [kg*m^2]";
  parameter Real iyy = 0.02590943825 "Gazebo gs_drone equivalent inertia yy [kg*m^2]";
  parameter Real izz = 0.045571756801 "Gazebo gs_drone equivalent inertia zz [kg*m^2]";
  parameter Real Ct = 8.54858e-6 "Thrust coefficient [N/(rad/s)^2]";
  parameter Real Cm = 0.016 "Rotor torque/thrust ratio [m]";
  parameter Real arm_length = 0.25 "Arm length [m]";
  parameter Real Cl_p = -0.2 "Rolling moment coefficient per roll rate";
  parameter Real Cm_q = -0.2 "Pitching moment coefficient per pitch rate";
  parameter Real Cn_r = -0.1 "Yawing moment coefficient per yaw rate";
  parameter Real tau_up = 0.0125 "Motor spin-up time constant [s]";
  parameter Real tau_down = 0.025 "Motor spin-down time constant [s]";

  RigidBody.Examples.QuadrotorSIL plant(
    ground_z = 0.0,
    vehicle_mass = mass,
    vehicle_ixx = ixx,
    vehicle_iyy = iyy,
    vehicle_izz = izz,
    Ct = Ct,
    Cm = Cm,
    arm_length = arm_length,
    Cl_p = Cl_p,
    Cm_q = Cm_q,
    Cn_r = Cn_r,
    tau_up = tau_up,
    tau_down = tau_down);

  parameter Real pwm_min = 1100.0 "Minimum motor PWM used by the Gazebo gs_drone ArduPilot control block";
  parameter Real pwm_max = 1900.0 "Maximum motor PWM used by the Gazebo gs_drone ArduPilot control block";
  parameter Real omega_min = 0.0 "Motor speed at minimum PWM [rad/s]";
  parameter Real omega_max = 1300.0 "Aerodynamic motor speed at maximum PWM [rad/s]";
  parameter Real lat0 = 40.414929 "Reference latitude [deg]";
  parameter Real lon0 = -86.932387 "Reference longitude [deg]";
  parameter Real ground_alt_wgs84 = 149.0 "WGS84 ellipsoid altitude of the local ground collision plane [m]";
  parameter Real accel_bias[3] = {0, 0, 0} "Accelerometer bias [m/s^2]";
  parameter Real gyro_bias[3] = {0, 0, 0} "Gyroscope bias [rad/s]";
  parameter Real mag_bias[3] = {0, 0, 0} "Magnetometer bias [Gauss]";
  parameter Real gps_bias[3] = {0, 0, 0} "GPS bias N/E/altitude [m]";
  parameter Real baro_alt_bias = 0.0 "Barometer relative altitude bias [m]";
  parameter Real earth_radius_m = 6378137.0 "Spherical Earth radius used for local geodetic conversion [m]";
  parameter Real pi = 3.141592653589793;

  input Real pwm[4](start = {1000, 1000, 1000, 1000}) "Motor PWM commands";

  output Real accel[3] "Body FRD accelerometer [m/s^2]";
  output Real gyro[3] "Body FRD gyroscope [rad/s]";
  output Real mag[3] "Body FRD magnetometer [Gauss]";
  output Real gps[3] "GPS latitude, longitude, altitude";
  output Real vel_ned[3] "GPS velocity NED [m/s]";
  output Real yaw_deg "Yaw [deg]";
  output Real roll_deg "Roll [deg]";
  output Real pitch_deg "Pitch [deg]";
  output Real baro_altitude_m "Barometer relative altitude [m]";
  output Real baro_pressure_pa "Barometer pressure [Pa]";
  output Real baro_temperature_c "Barometer temperature [degC]";
  output Real baro_climb_rate_mps "Barometer climb rate [m/s]";
  output Real motor_cmd[4] "Motor commands after PWM scaling [rad/s]";

protected
  Real pwm_span;
  Real pwm_norm[4];
  Real gps_lat_lon[2];
  Real yaw_rad;
  Real roll_rad;
  Real pitch_rad;

equation
  pwm_span = pwm_max - pwm_min;

  for i in 1:4 loop
    pwm_norm[i] = min(1.0, max(0.0, (pwm[i] - pwm_min) / pwm_span));
    motor_cmd[i] = omega_min + (omega_max - omega_min) * pwm_norm[i];
    plant.omega_cmd[i] = motor_cmd[i];
  end for;

  accel = plant.accel + accel_bias;
  gyro = plant.gyro + gyro_bias;
  mag = plant.mag + mag_bias;

  gps_lat_lon = Geodesy.localNorthEastToLatLon(
    lat0,
    lon0,
    plant.p[1] + gps_bias[1],
    -plant.p[2] + gps_bias[2],
    earth_radius_m);
  gps[1] = gps_lat_lon[1];
  gps[2] = gps_lat_lon[2];
  gps[3] = ground_alt_wgs84 + plant.p[3] + gps_bias[3];

  vel_ned[1] = plant.v_w[1];
  vel_ned[2] = -plant.v_w[2];
  vel_ned[3] = -plant.v_w[3];


  // radians are in NWU/FLU, degrees are converted to NED/FRD
  roll_rad = atan2(2.0 * (plant.q[1] * plant.q[2] + plant.q[3] * plant.q[4]),
                    1.0 - 2.0 * (plant.q[2] * plant.q[2] + plant.q[3] * plant.q[3]));
  roll_deg = roll_rad * 180.0 / pi;

  pitch_rad = asin(2.0 * (plant.q[1] * plant.q[3] - plant.q[4] * plant.q[2]));
  pitch_deg = -pitch_rad * 180.0 / pi;

  yaw_rad = atan2(2.0 * (plant.q[1] * plant.q[4] + plant.q[2] * plant.q[3]),
                  1.0 - 2.0 * (plant.q[3] * plant.q[3] + plant.q[4] * plant.q[4]));
  yaw_deg = -yaw_rad * 180.0 / pi;

  
  baro_altitude_m = plant.p[3] + baro_alt_bias;
  baro_temperature_c = 15.0 - 0.0065 * (ground_alt_wgs84 + baro_altitude_m);
  baro_pressure_pa = 101325.0 * (1.0 - 2.25577e-5 * (ground_alt_wgs84 + baro_altitude_m)) ^ 5.25588;
  baro_climb_rate_mps = plant.v_w[3];
end Copter;
