# Executed one line per NSH prompt by the host console bridge.
# PX4 v1.13 has one monolithic SIH sensor publisher rather than sensor_*_sim.
ver all
set R /
param set SYS_AUTOSTART 1100
sh /etc/init.d/airframes/1100_rc_quad_x_sih.hil
param set SYS_CTRL_ALLOC 1
param set COM_ARM_SDCARD 0
param set SDLOG_MODE -1
param set COM_RC_IN_MODE 4
param set IMU_GYRO_RATEMAX 250
param set SIH_DISTSNSR_OVR 10001
dataman start -r
sensors start -h
sih start
ekf2 start
pwm_out_sim start -m hil
load_mon start
commander start -h
sh /etc/init.d/rc.mc_apps
navigator start
mavlink start -d /dev/ttyS5 -b 921600 -m 0 -r 100000 -Z
mavlink boot_complete
echo FASTDYN_PX4_READY
