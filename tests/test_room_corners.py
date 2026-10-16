"""Room size from floor corners measured in the browser with WebXR."""
from backend.core import store
from backend.schemas import Room
from backend.stages import measure_room
from backend.stages.room_corners import room_from_corners
from tests.test_live import H, client  # noqa: F401  (pytest fixture)

FLOOR_Y = 0.0                                      # WebXR "local-floor": y = 0 at the floor, y is up
SQUARE = [[0, FLOOR_Y, 0], [4, FLOOR_Y, 0], [4, FLOOR_Y, 3], [0, FLOOR_Y, 3]]   # 4 m x 3 m, tapped in order


def test_square_room_tapped_in_order():
    r = room_from_corners(SQUARE, ceiling_y=2.5)
    assert (r.floor_area_m2, r.length_m, r.width_m, r.height_m, r.wall_area_m2) == (12.0, 4.0, 3.0, 2.5, 35.0)
    assert r.floor_area_ft2 == 129.2 and r.scale_method == "webxr_corners"


def test_l_shaped_room_keeps_its_shape():
    l_shape = [[0, 0, 0], [4, 0, 0], [4, 0, 2], [2, 0, 2], [2, 0, 4], [0, 0, 4]]      # 4x4 minus a 2x2 corner
    assert room_from_corners(l_shape, None).floor_area_m2 == 12.0


def test_out_of_order_taps_use_the_hull_and_say_so():
    r = room_from_corners([SQUARE[0], SQUARE[2], SQUARE[1], SQUARE[3]], None)
    assert r.floor_area_m2 == 12.0 and any("not tapped in order" in n for n in r.notes)
    assert r.height_m is None and r.wall_area_m2 is None                     # no height point: no guess


def test_implausible_height_is_ignored():
    r = room_from_corners(SQUARE, ceiling_y=0.6)
    assert r.height_m is None and any("height ignored" in n for n in r.notes)


def test_room_stage_prefers_measured_corners(tmp_path):
    sd = store.new_sweep("US", "USD", tmp_path)
    store.write(sd, "room_corners.json", {"corners": SQUARE, "ceiling_y": 2.5})
    measure_room.run(sd)
    assert store.read(sd, "room.json", Room).floor_area_m2 == 12.0


def test_corners_endpoint(client):
    sid = client.post("/api/sweeps", json={}, headers=H).json()["sweep_id"]
    r = client.post(f"/api/sweeps/{sid}/room/corners", json={"corners": SQUARE, "ceiling_y": 2.5}, headers=H)
    assert r.status_code == 200 and r.json()["floor_area_m2"] == 12.0
    assert client.post(f"/api/sweeps/{sid}/room/corners", json={"corners": SQUARE[:2]}, headers=H).status_code == 400
    assert client.post(f"/api/sweeps/{sid}/room/corners", json={"corners": [[0, 0, "x"]] * 3}, headers=H).status_code == 400
    assert client.post(f"/api/sweeps/{sid}/room/corners", json={"corners": SQUARE}).status_code == 401
