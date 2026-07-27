from livery_tracker.geo import haversine_nm


def test_zero_distance():
    assert haversine_nm(37.6213, -122.379, 37.6213, -122.379) == 0.0


def test_sfo_to_lax():
    # SFO -> LAX is ~293 NM great-circle
    dist = haversine_nm(37.6213, -122.3790, 33.9416, -118.4085)
    assert 285 < dist < 300


def test_symmetry():
    a = haversine_nm(37.6213, -122.379, 33.9416, -118.4085)
    b = haversine_nm(33.9416, -118.4085, 37.6213, -122.379)
    assert abs(a - b) < 1e-9
