"""Compare captured ground motion with each active navigation segment."""

import csv

import numpy as np
from mission_data import rover_legs


def rover_tracking(data, out, label="Left/right motor mixer", prefix="auto"):
    time, pos = data.time, data.pos
    speed, yawrate = data.v["vel_frd"][:, 0], data.v["omega_frd"][:, 2]
    legs = rover_legs(data.wp)
    targets = list(legs)
    if not targets:
        raise ValueError("No navigation targets in the captured mission")
    final_target = targets[-1]
    final = next(
        (
            float(e["fmu_s"])
            for e in data.events
            if e["event"] == "reached" and int(e["value"]) == final_target
        ),
        None,
    )
    start = data.lo
    end = final if final is not None else min(time[-1], float(data.events[-1]["fmu_s"]))
    active = [(start, targets[0])]
    for event in data.events:
        ts = float(event["fmu_s"])
        if event["event"] == "current" and start <= ts < end:
            seq = int(event["value"])
            if seq in legs and seq != active[-1][1]:
                active.append((ts, seq))
    ids_all = np.flatnonzero((time >= start) & (time <= end))
    if not len(ids_all):
        raise ValueError("No FMU samples between the recorded AUTO start and end")
    assigned = np.zeros(len(time), dtype=int)
    error = np.full(len(time), np.nan)
    rows = []
    for j, (enter, n) in enumerate(active):
        leave = active[j + 1][0] if j + 1 < len(active) else end
        ids = np.arange(
            np.searchsorted(time, enter),
            np.searchsorted(time, leave, side="left" if j + 1 < len(active) else "right"),
        )
        if not len(ids):
            continue
        i = targets.index(n)
        a, b = data.wp_ne[targets[i - 1] if i else 0], data.wp_ne[n]
        direction = b - a
        xy = pos[ids, :2]
        length2 = direction @ direction
        fraction = np.clip(((xy - a) @ direction) / length2, 0.0, 1.0) if length2 else np.zeros(len(ids))
        dist = np.linalg.norm(xy - a - fraction[:, None] * direction, axis=1)
        distance_to_target = np.linalg.norm(xy - b, axis=1)
        error[ids], assigned[ids] = dist, n
        rows.append(
            {
                "run": label,
                "target": n,
                "direction": "reverse" if legs[n] else "forward",
                "start_s": enter,
                "end_s": leave,
                "closest_to_target_m": distance_to_target.min(),
                "end_to_target_m": distance_to_target[-1],
                "segment_rms_m": np.sqrt(np.mean(dist**2)),
                "segment_p95_m": np.percentile(dist, 95),
                "segment_max_m": dist.max(),
                "speed_min_mps": speed[ids].min(),
                "speed_median_mps": np.median(speed[ids]),
                "speed_max_mps": speed[ids].max(),
            }
        )
    dist = error[ids_all]
    if not np.isfinite(dist).all():
        raise ValueError("AUTO samples could not be assigned to mission segments")
    pts = np.unique(np.r_[ids_all[:: max(1, round(0.05 / data.period))], ids_all[-1]])
    with (out / (prefix + "_track_20hz.csv")).open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(
            [
                "fmu_s",
                "auto_elapsed_s",
                "active_nav_target",
                "north_m",
                "east_m",
                "forward_speed_mps",
                "frd_yaw_rate_radps",
                "distance_to_active_segment_m",
            ]
        )
        wr.writerows(
            zip(
                time[pts],
                time[pts] - start,
                assigned[pts],
                pos[pts, 0],
                pos[pts, 1],
                speed[pts],
                yawrate[pts],
                error[pts],
            )
        )
    summary = {
        "start_s": start,
        "last_navigation_s": end,
        "elapsed_s": end - start,
        "final_item_reached": final is not None,
        "active_targets": [n for _, n in active],
        "final_target": final_target,
        "rms_m": np.sqrt(np.mean(dist**2)),
        "p95_m": np.percentile(dist, 95),
        "max_m": dist.max(),
        "final_target_distance_m": np.linalg.norm(pos[ids_all[-1], :2] - data.wp_ne[final_target]),
        "speed_min_mps": speed[ids_all].min(),
        "speed_max_mps": speed[ids_all].max(),
        "yaw_rate_min_radps": yawrate[ids_all].min(),
        "yaw_rate_max_radps": yawrate[ids_all].max(),
    }
    return summary, rows


def motion_signs(data, out, end):
    time = data.time
    dt = np.diff(time)
    active = (time[1:] >= data.lo) & (time[1:] <= end)
    velocity = np.diff(data.pos, axis=0) / dt[:, None]
    forward = np.sum(velocity * data.rot[1:, :, 0], axis=1)
    heading = np.unwrap(np.arctan2(data.rot[:, 1, 0], data.rot[:, 0, 0]))
    rows = []
    for name, observed, derived, unit, threshold in [
        ("Forward speed vs projected position change", data.v["vel_frd"][1:, 0], forward, "m/s", 0.1),
        (
            "Down-axis yaw rate vs heading change",
            data.v["omega_frd"][1:, 2],
            np.diff(heading) / dt,
            "rad/s",
            0.02,
        ),
    ]:
        use = active & (np.abs(observed) > threshold)
        rows.append(
            {
                "check": name,
                "unit": unit,
                "minimum_absolute_signal": threshold,
                "positive_samples": np.sum(use & (observed > 0)),
                "negative_samples": np.sum(use & (observed < 0)),
                "sign_mismatches": np.count_nonzero(np.sign(observed[use]) != np.sign(derived[use])),
                "max_absolute_difference": np.max(abs(observed[use] - derived[use]))
                if np.any(use)
                else None,
            }
        )
    write_rows(out / "motion_sign_checks.csv", rows)
    return [row["check"] for row in rows if row["sign_mismatches"]]


def write_rows(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
