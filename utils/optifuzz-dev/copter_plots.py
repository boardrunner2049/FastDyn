from functools import partial

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from plot_common import (
    colors,
    dark,
    fmt,
    footer,
    gold,
    head,
    page,
    plot_series,
    plt,
    state_pages,
)


def write_plots(data, out, summary):
    time, v, pos, wp_ne = data.time, data.v, data.pos, data.wp_ne
    lo, hi = data.lo, data.hi
    _te, en = data.telemetry()
    sl = np.flatnonzero((time >= lo) & (time <= hi))[::10]
    series = partial(plot_series, time, sl)
    alt, gps_pos = data.alt, data.gps_pos
    t0 = np.flatnonzero(time - time[0] < 0.27)
    idx = np.unique(np.r_[0, np.arange(0, len(time), 50), len(time) - 1])
    with PdfPages(out / "copter462_plots_en.pdf") as pdf:
        fig = plt.figure(figsize=(12.2, 7.1))
        gs = fig.add_gridspec(
            2,
            2,
            left=0.075,
            right=0.967,
            top=0.785,
            bottom=0.11,
            width_ratios=[1.05, 1],
            wspace=0.30,
            hspace=0.54,
        )
        amap = fig.add_subplot(gs[:, 0])
        ah = fig.add_subplot(gs[0, 1])
        an = fig.add_subplot(gs[1, 1])
        head(
            fig,
            "Copter 4.6.2 mission trajectory",
            f"Local origin ({data.p['lat0']:.6f}°, {data.p['lon0']:.6f}°). North is up. Height is relative to the ground plane.",
        )
        plan = np.array(list(wp_ne.values()))
        amap.plot(plan[:, 1], plan[:, 0], "--", color="#85949a", lw=1.5, label="Planned route")
        amap.plot(pos[idx, 1], pos[idx, 0], color=colors[0], lw=2.4, label="FMU pos_ned")
        active = np.flatnonzero(time > lo)[::1500]
        amap.scatter(
            gps_pos[active, 1],
            gps_pos[active, 0],
            s=16,
            facecolors="white",
            edgecolors=colors[0],
            lw=1,
            label="FMU GPS projected to local axes",
            zorder=4,
        )
        amap.scatter(
            en[::3, 1],
            en[::3, 0],
            s=8,
            color=colors[1],
            alpha=0.70,
            label="ArduPilot position estimate",
            zorder=3,
        )
        for row in data.wp:
            j = int(row[0])
            cmd = int(row[3])
            if j == 0 or cmd not in (16, 22):
                continue
            label = "Takeoff" if cmd == 22 else f"WP{j}"
            dx, dy = 5, 5
            e, n = wp_ne[j][1], wp_ne[j][0]
            amap.scatter(e, n, marker="D", s=32, edgecolor=dark, facecolor="white", zorder=5)
            amap.annotate(
                label,
                (e, n),
                xytext=(dx, dy),
                textcoords="offset points",
                fontsize=9,
                weight="bold",
                zorder=6,
            )
        amap.set_aspect("equal")
        amap.margins(0.20)
        fmt(amap, "Ground track", "East (m)", "North (m)")
        amap.legend(loc="upper right", ncol=1, fontsize=8.4)
        ah.plot(time[idx], alt[idx], color=colors[0], lw=1.8, label="FMU height (−D)")
        heights = data.wp[np.isin(data.wp[:, 3], [16, 22]) & (data.wp[:, 2] == 3), 10]
        if len(heights) and np.allclose(heights, heights[0]):
            ah.axhline(
                heights[0], color=gold, ls="--", lw=1.05, label=f"Cruise target ({heights[0]:g} m)"
            )
        ah.set_xlim(0, time[-1] + 1)
        ah.legend(loc="center left")
        fmt(ah, "Height profile", "Simulation time (s)", "Height above ground (m)")
        an.plot(time[sl], pos[sl, 0], color=colors[0], label="North", lw=1.6)
        an.plot(time[sl], pos[sl, 1], color=colors[1], label="East", lw=1.6)
        for row in summary["closest_approach"]:
            t = row["time_s"]
            an.axvline(t, color="#838f98", ls=":", lw=0.8)
            an.text(
                t,
                0.98,
                "  WP" + str(row["waypoint"]),
                transform=an.get_xaxis_transform(),
                va="top",
                fontsize=7.4,
            )
        an.legend(loc="lower right", ncol=2)
        fmt(an, "Horizontal position during flight", "Simulation time (s)", "Displacement (m)")
        footer(fig, 1, "copter")
        fig.savefig(out / "trajectory_en.png", dpi=155)
        pdf.savefig(fig)
        plt.close(fig)

        state_pages(pdf, data)
        fig, ax = page(
            "Internal plant dynamics",
            "The top row shows the first 0.27 s at 1 kHz. The bottom row covers the flight interval.",
        )
        series(
            ax[0],
            v["accel_ned"][:, 2],
            ["NED D"],
            "Initial contact / downward acceleration",
            "m/s²",
            points=t0,
        )
        series(
            ax[1],
            v["plant.F_ground_b"][:, 2],
            ["FLU U"],
            "Vertical landing-gear reaction",
            "N",
            points=t0,
        )
        series(
            ax[2], v["plant.q"], ["w", "x", "y", "z"], "plant.q  /  FLU to NWU quaternion", "Coefficient"
        )
        series(ax[3], v["plant.M_b"], ["F", "L", "U"], "plant.M_b  /  body-axis moment", "N·m")
        footer(fig, 6, "copter")
        pdf.savefig(fig)
        plt.close(fig)
