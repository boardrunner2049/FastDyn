from functools import partial

import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from mission_data import rover_legs
from plot_common import (
    colors,
    dark,
    fmt,
    footer,
    head,
    page,
    plot_series,
    plt,
    state_pages,
)


def write_plots(data, out, summary):
    time, v, _pos, wp_ne = data.time, data.v, data.pos, data.wp_ne
    lo, hi = data.lo, data.hi
    te, en = data.telemetry()
    sl = np.flatnonzero((time >= lo) & (time <= hi))[::10]
    series = partial(plot_series, time, sl)
    stats = summary["successful_run"]
    with PdfPages(out / "rover462_auto_en.pdf") as pdf:
        track = np.genfromtxt(out / "auto_track_20hz.csv", names=True, delimiter=",")
        legs = rover_legs(data.wp)
        ordered = [0, *legs]
        plan = np.array([wp_ne[i] for i in ordered])
        reverse = np.isin(track["active_nav_target"], [n for n in legs if legs[n]])
        t = track["auto_elapsed_s"]
        fig = plt.figure(figsize=(12.2, 8.15))
        gs = fig.add_gridspec(
            3,
            2,
            left=0.082,
            right=0.965,
            top=0.80,
            bottom=0.13,
            width_ratios=[1.0, 1.28],
            wspace=0.33,
            hspace=0.48,
        )
        amap = fig.add_subplot(gs[:, 0])
        amap.plot(
            plan[:, 1], plan[:, 0], ls="--", lw=1.35, color="#43535b", label="Uploaded route", zorder=3
        )
        mask = (te[:, 0] >= lo) & (te[:, 0] <= summary["successful_run"]["last_navigation_s"])
        amap.scatter(
            en[mask][::3, 1],
            en[mask][::3, 0],
            marker=".",
            s=11,
            color="#80698c",
            label="ArduRover EKF",
            zorder=2,
        )
        for active, c, label, ls in [
            (~reverse, colors[0], "Forward legs", "-"),
            (reverse, colors[1], "Reverse legs", "-"),
        ]:
            amap.plot(
                np.where(active, track["east_m"], np.nan),
                track["north_m"],
                c=c,
                lw=2.5,
                ls=ls,
                label=label,
                zorder=4,
            )
        for n in ordered:
            offset = (5, 4) if wp_ne[n][1] >= 0 else (-26, 3)
            north, east = wp_ne[n]
            amap.scatter(east, north, marker="D", s=25, edgecolor=dark, facecolor="white", zorder=5)
            amap.annotate(
                "Home" if n == 0 else f"WP{n}",
                (east, north),
                xytext=offset,
                textcoords="offset points",
                fontsize=8,
                weight="bold",
            )
        amap.set_aspect("equal")
        amap.margins(0.22)
        fmt(amap, "Planned and measured ground track", "East (m)", "North (m)")
        amap.legend(loc="upper center", bbox_to_anchor=(0.53, -0.105), ncol=2, fontsize=8)
        head(
            fig,
            "Rover AUTO mission trajectory",
            "ArduRover 4.6.2  ·  Navigation targets and drive direction from the uploaded mission  ·  Left/right motor interface",
        )
        panels = []
        for j, (col, label, unit, color) in enumerate(
            [
                ("forward_speed_mps", "Body forward speed", "m/s", colors[0]),
                ("frd_yaw_rate_radps", "Body yaw rate  /  positive turns right", "rad/s", colors[2]),
                (
                    "distance_to_active_segment_m",
                    "Distance to the assigned route segment",
                    "m",
                    colors[1],
                ),
            ]
        ):
            ax = fig.add_subplot(gs[j, 1])
            panels.append(ax)
            for span in np.ma.clump_unmasked(np.ma.masked_where(~reverse, t)):
                a, b = t[span][[0, -1]]
                ax.axvspan(a, b, color="#f5decc", alpha=0.72)
                if j == 0:
                    ax.text(
                        (a + b) / 2,
                        0.98,
                        "Reverse",
                        transform=ax.get_xaxis_transform(),
                        ha="center",
                        va="top",
                        fontsize=8,
                    )
            ax.plot(t, track[col], c=color, lw=1.45)
            ax.axhline(0, lw=0.6, c="#58646a")
            ax.set_xlim(0, t[-1])
            ax.set_ylabel(unit)
            ax.set_title(label, fontsize=11)
            ax.set_xlabel("Time since AUTO start (s)" if j == 2 else "")
        fig.text(
            0.965,
            0.044,
            f"Segment error   RMS {stats['rms_m']:.2f} m   /   95th percentile {stats['p95_m']:.2f} m   /   maximum {stats['max_m']:.2f} m",
            ha="right",
            fontsize=9,
            color=dark,
        )
        footer(fig, 1, "rover")
        fig.savefig(out / "trajectory_en.png", dpi=165)
        pdf.savefig(fig)
        plt.close(fig)

        state_pages(pdf, data)
        fig, ax = page(
            "Rover driveline and yaw dynamics",
            "Open circles show same-tick plant values. Both wheel speeds are derived from the same body forward speed in RoverPlant.",
            3,
            2,
        )
        series(
            ax[0],
            v["motor_cmd"][:, 2],
            ["forward speed"],
            "motor_cmd[3]  /  positive forward",
            "m/s",
            v["plant.v_b"][:, 0],
        )
        series(
            ax[1],
            v["motor_cmd"][:, 3],
            ["yaw rate"],
            "motor_cmd[4]  /  positive right",
            "rad/s",
            -v["plant.omega"][:, 2],
        )
        series(
            ax[2],
            v["plant.wheel_omega"],
            ["left", "right"],
            "plant.wheel_omega  /  signed wheel speed",
            "rad/s",
        )
        series(
            ax[3],
            np.column_stack((v["plant.target_yaw_rate"], v["plant.omega"][:, 2])),
            ["demand", "measured"],
            "Yaw response in the internal FLU frame",
            "rad/s",
        )
        series(ax[4], v["plant.M_b"], ["F", "L", "U"], "plant.M_b  /  body moments", "N·m")
        series(
            ax[5], v["plant.q"], ["w", "x", "y", "z"], "plant.q  /  FLU to NWU quaternion", "Coefficient"
        )
        footer(fig, 6, "rover")
        pdf.savefig(fig)
        plt.close(fig)

        if "conventional_run" not in summary:
            return
        baseline = np.genfromtxt(out / "conventional_track_20hz.csv", names=True, delimiter=",", ndmin=1)
        fig = plt.figure(figsize=(12.2, 8.1))
        head(
            fig,
            "Actuator interface and waypoint tracking",
            "Two closed-loop runs use the same uploaded mission and ground origin. Both use neutral PWM during boot.",
        )
        gs = fig.add_gridspec(
            2,
            2,
            left=0.09,
            right=0.96,
            top=0.80,
            bottom=0.13,
            height_ratios=[1.0, 0.85],
            wspace=0.32,
            hspace=0.50,
        )
        ax = fig.add_subplot(gs[:, 0])
        ax.plot(
            baseline["east_m"],
            baseline["north_m"],
            color=colors[1],
            lw=1.6,
            label="Conventional steering",
        )
        ax.plot(track["east_m"], track["north_m"], color=colors[0], lw=2, label="Left/right mixer")
        ax.plot(plan[:, 1], plan[:, 0], color="#525f66", lw=1.1, ls="--", label="Uploaded route")
        ax.set_aspect("equal")
        fmt(ax, "Ground track at the same map scale", "East (m)", "North (m)")
        ax.legend(loc="upper right", fontsize=9)
        pane = fig.add_subplot(gs[0, 1])
        for series_data, c, name in [
            (baseline, colors[1], "Conventional"),
            (track, colors[0], "Left/right mixer"),
        ]:
            pane.plot(
                series_data["auto_elapsed_s"],
                series_data["distance_to_active_segment_m"],
                c=c,
                lw=1.6,
                label=name,
            )
        fmt(pane, "Route-segment error", "Time since AUTO start (s)", "Distance (m)")
        pane.legend()
        pane = fig.add_subplot(gs[1, 1])
        pane.set_axis_off()
        stats = summary["successful_run"]
        base = summary["conventional_run"]
        lines = [
            ["Interface", "Last active WP", "RMS (m)"],
            ["Conventional steering", str(base["active_targets"][-1]), f"{base['rms_m']:.2f}"],
            ["Left/right mixer", str(stats["active_targets"][-1]), f"{stats['rms_m']:.2f}"],
        ]
        tbl = pane.table(
            cellText=lines[1:],
            colLabels=lines[0],
            cellLoc="left",
            colLoc="left",
            bbox=[0, 0.54, 1, 0.46],
            colWidths=[0.49, 0.30, 0.21],
        )
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(9)
        for (ii, jj), cell in tbl.get_celld().items():
            cell.set_edgecolor("#ccd7d9")
            if ii == 0:
                cell.set_facecolor("#e4eeef")
                cell.set_text_props(weight="bold")
        pane.text(
            0,
            0.45,
            f"Conventional steering observed for {base['elapsed_s']:.2f} s.\nLeft/right mixer observed for {stats['elapsed_s']:.2f} s.\nFinal target distance with mixer: {stats['final_target_distance_m']:.2f} m.",
            va="top",
            fontsize=10,
            linespacing=1.55,
        )
        fig.text(
            0.09,
            0.068,
            "Each RMS uses its own run duration. Distances use the finite line segment ending at the active navigation target.",
            fontsize=9,
            color="#506773",
        )
        footer(fig, 7, "rover")
        pdf.savefig(fig)
        plt.close(fig)
