"""Track-gate fixtures. These samples have no performance role."""
import pytest

from oracle import check_track


LIMITS = {"waypoint_radius_m": 5, "waypoint_altitude_tolerance_m": 5, "landing_height_m": 1}
WAYPOINTS = [(40., -86., 15.), (40.001, -85.999, 15.), (40.002, -86., 15.)]
TRACK = [(40., -86., 149.), (40., -86., 164.), (40.001, -85.999, 164.),
         (40.002, -86., 164.), (40., -86., 149.)]


def test_trajectory_and_ground_datum():
    check_track(TRACK, WAYPOINTS, LIMITS, 149.)


@pytest.mark.parametrize("indices", [(0, 4), (0, 1, 3, 2, 4), (0, 1, 2, 3)])
def test_incomplete_out_of_order_or_airborne_finish(indices):
    with pytest.raises(ValueError):
        check_track([TRACK[i] for i in indices], WAYPOINTS, LIMITS, 149.)


def test_frozen_inventory_is_independent_of_late_appends(tmp_path):
    from baseline import digest, freeze_inventory

    cache = tmp_path / "mission.sc"
    with cache.open("wb") as stream:
        stream.write(b"header")
        stream.seek(1024 * 1024)
        stream.write(b"records")
    manifest = tmp_path / "mission.sc.fastdyn.json"
    manifest.write_text('{"fingerprint":"example"}')
    identity = freeze_inventory(cache, manifest, tmp_path)
    snapshot = tmp_path / "frozen" / "mission.sc"
    assert identity["cache_sha256"] == digest(cache) == digest(snapshot)
    assert identity["logical_bytes"] == cache.stat().st_size
    with cache.open("ab") as stream:
        stream.write(b"new translation")
    assert digest(snapshot) == identity["cache_sha256"] != digest(cache)
    assert snapshot.stat().st_mode & 0o222 == 0
