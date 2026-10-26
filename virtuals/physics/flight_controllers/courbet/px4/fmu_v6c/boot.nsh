# Executed one line per NSH prompt by the host console bridge.
# Run the original ARM/NuttX modules. The FMU adapter replaces sensor samples.
ver all
set R /
param set SYS_AUTOSTART 1100
sh /etc/init.d/airframes/1100_rc_quad_x_sih.hil
param set COM_ARM_SDCARD 0
param set SDLOG_MODE -1
# Autonomous simulated vehicle without an RC/manual-control source.
param set COM_RC_IN_MODE 4
param set IMU_GYRO_RATEMAX 250
# Disable the SIH distance sensor: it is not connected to the FMU.
param set SIH_DISTSNSR_OVR 10001
dataman start -r
sensors start -h
simulator_sih start
sensor_baro_sim start
sensor_mag_sim start
sensor_gps_sim start
ekf2 start
pwm_out_sim start -m hil
load_mon start
commander start -h
sh /etc/init.d/rc.mc_apps
navigator start
# TELEM1 on FMUv6C; raw bytes are forwarded by the host UART adapter.
mavlink start -d /dev/ttyS5 -b 921600 -m 0 -r 100000 -Z
mavlink boot_complete
echo FASTDYN_PX4_READY
