from functools import partial

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from mission_data import FRAME_SIGN, mult
from scipy.spatial.transform import Rotation

plt.rcParams.update(
    {
        "font.family": ["DejaVu Sans"],
        "font.size": 10,
        "pdf.fonttype": 42,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.titlelocation": "left",
        "axes.titleweight": "bold",
        "axes.labelcolor": "#203b49",
        "text.color": "#203b49",
        "axes.edgecolor": "#9aabb0",
        "axes.grid": True,
        "grid.color": "#dde5e8",
        "grid.linewidth": 0.7,
        "axes.axisbelow": True,
        "legend.frameon": False,
        "legend.fontsize": 8.5,
        "figure.facecolor": "white",
    }
)
colors = ["#147378", "#b84926", "#5469ab", "#914e80"]
dark = "#203b49"
gold = "#a86a04"


def head(fig, title, sub):
    fig.text(0.075, 0.955, title, fontsize=21, weight="bold", va="top")
    fig.text(0.075, 0.888, sub, fontsize=10.3, va="top", color="#475f69")


def page(title, sub, nrow=2, ncol=2):
    fig, axes = plt.subplots(nrow, ncol, figsize=(12, 8.1), squeeze=False)
    fig.subplots_adjust(top=0.806, bottom=0.082, left=0.078, right=0.965, hspace=0.53, wspace=0.42)
    head(fig, title, sub)
    return fig, axes.ravel()


def footer(fig, n, vehicle):
    if n in (2, 4, 5, 6):
        for ax in fig.axes:
            if ax.get_subplotspec() is not None and ax.get_subplotspec().rowspan.start < 2:
                ax.set_xlabel("")
    fig.text(
        0.965,
        0.022,
        f"FastDyn.{vehicle.title()}  ·  AUTO mission  /  {n}",
        ha="right",
        fontsize=8,
        color="#607883",
    )


def plot_series(time, default_points, ax, ys, names, title, unit, ref=None, points=None):
    if points is None:
        points = default_points
    ys = np.asarray(ys).reshape(len(time), -1)
    if ref is not None:
        ref = np.asarray(ref).reshape(len(time), -1)
    ax.set_title(title, fontsize=11)
    for i, n in enumerate(names):
        c = colors[i % len(colors)]
        ax.plot(time[points], ys[points, i], lw=1.15, label=n, color=c)
        if ref is not None:
            dots = points[:: max(1, len(points) // 28)]
            ax.plot(time[dots], ref[dots, i], ls="none", marker="o", ms=3, mfc="white", mec=c, mew=0.85)
    ax.set_ylabel(unit)
    ax.set_xlabel("Simulation time (s)")
    ax.margins(x=0.008)
    ax.legend(ncol=min(4, len(names)), loc="best")


def fmt(ax, title, x, y):
    ax.set_title(title, fontsize=12)
    ax.set_xlabel(x)
    ax.set_ylabel(y)


def state_pages(pdf, data):
    time, v, p, rot, pos, gps = data.time, data.v, data.p, data.rot, data.pos, data.gps
    s = FRAME_SIGN
    sl = np.flatnonzero((time >= data.lo) & (time <= data.hi))[::10]
    series = partial(plot_series, time, sl)
    fig, ax = page(
        "Translation in world and body frames",
        "Lines show FMU outputs. Circles use internal plant states. The transform S preserves the first axis and reverses the second and third.",
        3,
        2,
    )
    series(ax[0], pos, ["N", "E", "D"], "pos_ned  /  S · plant.p", "m", v["plant.p"] * s)
    series(ax[1], v["plant.p"], ["N", "W", "U"], "plant.p  /  internal NWU position", "m")
    series(ax[2], v["vel_ned"], ["N", "E", "D"], "vel_ned  /  S · plant.v_w", "m/s", v["plant.v_w"] * s)
    series(ax[3], v["vel_frd"], ["F", "R", "D"], "vel_frd  /  S · plant.v_b", "m/s", v["plant.v_b"] * s)
    series(
        ax[4],
        v["accel_ned"],
        ["N", "E", "D"],
        "accel_ned  /  rotated specific force plus gravity",
        "m/s²",
        mult(rot, v["plant.accel"]) + [0, 0, p["plant.g"]],
    )
    aflu = v["der(plant.v_b)"] + np.cross(v["plant.omega"], v["plant.v_b"])
    series(
        ax[5],
        v["accel_frd"],
        ["F", "R", "D"],
        "accel_frd  /  kinematic acceleration in body axes",
        "m/s²",
        aflu * s,
    )
    footer(fig, 2, data.vehicle)
    pdf.savefig(fig)
    plt.close(fig)

    fig, ax = page(
        "Attitude and angular motion",
        "Open circles come from plant attitude and angular dynamics. Positive FRD yaw turns north toward east.",
    )
    re = Rotation.from_matrix(data.attitude_from_quat()).as_euler("ZYX")[:, ::-1]
    series(
        ax[0],
        np.degrees(v["rpy"][:, :2]),
        ["roll", "pitch"],
        "rpy  /  roll and pitch",
        "deg",
        np.degrees(re[:, :2]),
    )
    wrapped = v["yaw_deg"]
    unwrapped = np.degrees(np.unwrap(v["rpy"][:, 2]))
    series(
        ax[1],
        np.column_stack([wrapped, unwrapped]),
        ["yaw_deg", "unwrap(rpy yaw)"],
        "yaw  /  wrapped and continuous angles",
        "deg",
    )
    series(
        ax[2],
        v["omega_frd"],
        ["F", "R", "D"],
        "omega_frd  /  S · plant.omega",
        "rad/s",
        v["plant.omega"] * s,
    )
    series(
        ax[3],
        v["alpha_frd"],
        ["F", "R", "D"],
        "alpha_frd  /  S · der(plant.omega)",
        "rad/s²",
        v["der(plant.omega)"] * s,
    )
    footer(fig, 3, data.vehicle)
    pdf.savefig(fig)
    plt.close(fig)

    fig, ax = page(
        "Rotation from body FRD to world NED",
        "Rows are north, east and down. Columns are forward, right and down. Open circles are reconstructed from the plant quaternion.",
        3,
        3,
    )
    rq = data.attitude_from_quat()
    for i in range(3):
        for j in range(3):
            pane = ax[3 * i + j]
            key = f"R[{i + 1},{j + 1}]"
            series(pane, rot[:, i, j], [key], f"{'NED'[i]}  ←  {'FRD'[j]}", "Coefficient", rq[:, i, j])
            pane.get_legend().remove()
    footer(fig, 4, data.vehicle)
    pdf.savefig(fig)
    plt.close(fig)

    fig, ax = page(
        "Sensors and actuators",
        "GPS offsets use the local origin. Pressure, temperature and height come from the FMU barometer model.",
        3,
        2,
    )
    series(
        ax[0],
        gps[:, :2] - [p["lat0"], p["lon0"]],
        ["latitude", "longitude"],
        "pos_gps  /  latitude and longitude offsets",
        "deg",
    )
    series(
        ax[1],
        np.column_stack((gps[:, 2], p["ground_alt_wgs84"] + v["baro_altitude_m"])),
        ["pos_gps[3]", "ground + baro_altitude_m"],
        "GPS and barometric height",
        "m",
    )
    series(
        ax[2], v["mag"], ["F", "R", "D"], "mag  /  body magnetic field", "G", v["plant.mag_b_flu"] * s
    )
    series(
        ax[3],
        v["baro_climb_rate_mps"],
        ["climb"],
        "baro_climb_rate_mps  /  climb rate",
        "m/s",
        -v["vel_ned"][:, 2],
    )
    pane = ax[4]
    series(
        pane, v["baro_pressure_pa"] / 1000, ["pressure"], "baro_pressure_pa · baro_temperature_c", "kPa"
    )
    pane.get_legend().remove()
    other = pane.twinx()
    other.grid(False)
    other.plot(
        time[sl], v["baro_temperature_c"][sl], color=colors[1], ls="--", lw=1.2, label="temperature"
    )
    other.set_ylabel("°C", color=colors[1])
    pane.legend([pane.lines[0], other.lines[0]], ["pressure", "temperature"], loc="best", ncol=2)
    if data.vehicle == "rover":
        series(
            ax[5],
            np.column_stack((v["motor_cmd"][:, 0], v["motor_cmd"][:, 1])),
            ["steering", "throttle"],
            "motor_cmd[1–2]  /  actuator demand",
            "Normalized",
            np.column_stack((v["plant.steering"], v["plant.throttle"])),
        )
    else:
        series(
            ax[5],
            v["motor_cmd"],
            ["1", "2", "3", "4"],
            "motor_cmd  /  command and rotor speed",
            "rad/s",
            v["plant.omega_m"],
        )
    footer(fig, 5, data.vehicle)
    pdf.savefig(fig)
    plt.close(fig)
