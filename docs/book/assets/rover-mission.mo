within FastDyn;

model Rover
  RigidBody.Examples.RoverPlant plant(mag_world_enu = {0.21, 0, -0.45});

  parameter Real pwm_min = 1000.0 "Minimum PWM";
  parameter Real pwm_trim = 1500.0 "Neutral PWM";
  parameter Real pwm_max = 2000.0 "Maximum PWM";
  parameter Real lat0 = 40.414929 "Reference latitude [deg]";
  parameter Real lon0 = -86.932387 "Reference longitude [deg]";
  parameter Real ground_alt_wgs84 = 149.0 "WGS84 ellipsoid altitude of the local ground plane [m]";
  parameter Real accel_bias[3] = {0, 0, 0} "Accelerometer bias [m/s^2]";
  parameter Real gyro_bias[3] = {0, 0, 0} "Gyroscope bias [rad/s]";
  parameter Real mag_bias[3] = {0, 0, 0} "Magnetometer bias [Gauss]";
  parameter Real gps_bias[3] = {0, 0, 0} "GPS bias N/E/altitude [m]";
  parameter Real baro_alt_bias = 0.0 "Barometer relative altitude bias [m]";
  parameter Real earth_radius_m = 6378137.0 "Spherical Earth radius used for local geodetic conversion [m]";
  parameter Real pi = 3.141592653589793;

  input Real pwm[4](start = {1500, 1500, 1500, 1500}) "Servo PWM commands";

  output Real accel[3] "Body FRD accelerometer [m/s^2]";
  output Real gyro[3] "Body FRD gyroscope [rad/s]";
  output Real mag[3] "Body FRD magnetometer [Gauss]";
  output Real gps[3] "GPS latitude, longitude, altitude";
  output Real vel_ned[3] "GPS velocity NED [m/s]";
  output Real yaw_deg "Yaw [deg]";
  output Real baro_altitude_m "Barometer relative altitude [m]";
  output Real baro_pressure_pa "Barometer pressure [Pa]";
  output Real baro_temperature_c "Barometer temperature [degC]";
  output Real baro_climb_rate_mps "Barometer climb rate [m/s]";
  output Real motor_cmd[4] "Normalized steering/throttle commands";

protected
  Real steering;
  Real throttle;
  Real gps_lat_lon[2];
  Real geodetic_origin[3] "Reference latitude, longitude, and Earth radius";
  Real yaw_rad;

equation
  steering = min(1.0, max(-1.0, (pwm[1] - pwm_trim) / (pwm_max - pwm_trim)));
  throttle = min(1.0, max(-1.0, (pwm[3] - pwm_trim) / (pwm_max - pwm_trim)));
  plant.steering = steering;
  plant.throttle = throttle;

  accel = {plant.accel[1], -plant.accel[2], -plant.accel[3]} + accel_bias;
  gyro = {plant.gyro[1], -plant.gyro[2], -plant.gyro[3]} + gyro_bias;
  mag = {plant.mag[1], -plant.mag[2], -plant.mag[3]} + mag_bias;

  // Avoid collisions between the caller parameters and the function locals
  // during function projection in the pinned Rumoca compiler.
  geodetic_origin = {lat0, lon0, earth_radius_m};
  gps_lat_lon = Geodesy.localNorthEastToLatLon(
    geodetic_origin[1],
    geodetic_origin[2],
    plant.p[1] + gps_bias[1],
    -plant.p[2] + gps_bias[2],
    geodetic_origin[3]);
  gps[1] = gps_lat_lon[1];
  gps[2] = gps_lat_lon[2];
  gps[3] = ground_alt_wgs84 + plant.p[3] + gps_bias[3];

  vel_ned[1] = plant.v_w[1];
  vel_ned[2] = -plant.v_w[2];
  vel_ned[3] = -plant.v_w[3];

  yaw_rad = atan2(2.0 * (plant.q[1] * plant.q[4] + plant.q[2] * plant.q[3]),
                  1.0 - 2.0 * (plant.q[3] * plant.q[3] + plant.q[4] * plant.q[4]));
  yaw_deg = -yaw_rad * 180.0 / pi;

  baro_altitude_m = plant.p[3] + baro_alt_bias;
  baro_temperature_c = 15.0 - 0.0065 * (ground_alt_wgs84 + baro_altitude_m);
  baro_pressure_pa = 101325.0 * (1.0 - 2.25577e-5 * (ground_alt_wgs84 + baro_altitude_m)) ^ 5.25588;
  baro_climb_rate_mps = plant.v_w[3];
  motor_cmd = {steering, throttle, plant.v_b[1], plant.omega[3]};
end Rover;
