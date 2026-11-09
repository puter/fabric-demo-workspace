import json
from datetime import timedelta

import pytest

from mvoi.geo.reference import Facility
from mvoi.risk.alerts import AlertManager
from mvoi.risk.scoring import ActiveEvent, distance_weight, score_facility
from tests.conftest import T0

FAC = Facility("F1", "Test Site", "venue", 39.80, -84.20, "Dayton", 3,
               "I-75", "1 Test St", "", False)


def _inc(key, lat=39.80, lon=-84.20, sevr="major", closure=False, etype="incident"):
    return ActiveEvent(key, etype, sevr, lat, lon, closure, title="Crash on I-75")


def _wx(key, cap="Severe", covers=True):
    lon0 = -84.3 if covers else -83.0
    ring = [[lon0, 39.7], [lon0 + 0.2, 39.7], [lon0 + 0.2, 39.9], [lon0, 39.9], [lon0, 39.7]]
    poly = json.dumps({"type": "Polygon", "coordinates": [ring]})
    return ActiveEvent(key, "weather_alert", "major", None, None, geometry_geojson=poly,
                       category="Severe Thunderstorm Warning", cap_severity=cap)


def test_distance_weight(risk_cfg):
    assert distance_weight(0, risk_cfg) == 1
    assert distance_weight(1600, risk_cfg) == 1
    assert distance_weight(4800, risk_cfg) == pytest.approx(0.5)
    assert distance_weight(8000, risk_cfg) == 0


def test_no_exposure_scores_zero(risk_cfg):
    r = score_facility(FAC, [], risk_cfg, T0)
    assert r.score == 0 and r.band == "low"
    assert "no active" in r.explanation


def test_components_and_explanation(risk_cfg):
    r = score_facility(FAC, [_inc("a", closure=True), _wx("w")], risk_cfg, T0)
    assert r.incident_points == 20 and r.road_closure_points == 15 and r.weather_points == 25
    assert r.score == pytest.approx(60.0)  # criticality 3 -> x1.0
    assert "road closure" in r.explanation and "Severe Thunderstorm Warning" in r.explanation
    assert "not a statement about the site" in r.explanation
    assert r.exposure_type == "inferred"
    assert r.contributors[0].points >= r.contributors[-1].points


def test_caps_are_enforced(risk_cfg):
    many = [_inc(str(i), sevr="critical") for i in range(10)]
    r = score_facility(FAC, many, risk_cfg, T0)
    assert r.incident_points == risk_cfg.incident.cap


def test_far_events_ignored_and_weather_outside_polygon_ignored(risk_cfg):
    r = score_facility(FAC, [_inc("far", lat=40.2), _wx("w", covers=False)], risk_cfg, T0)
    assert r.score == 0


def test_criticality_multiplier_and_ceiling(risk_cfg):
    crit5 = Facility(**{**FAC.__dict__, "criticality": 5})
    evs = [_inc(str(i), sevr="critical", closure=True) for i in range(3)] + [_wx("w", "Extreme")]
    r = score_facility(crit5, evs, risk_cfg, T0, history_occurrences=50)
    assert r.score == 100 and r.band == "severe"


def test_history_component(risk_cfg):
    r = score_facility(FAC, [], risk_cfg, T0, history_occurrences=4)
    assert r.history_points == 6.0


# ------------------------------------------------------------------- alerts


def _risk(score, fid="F1"):
    from mvoi.risk.scoring import FacilityRisk
    return FacilityRisk(fid, "Test", T0, score, "high", score, 3, 1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        None, f"score {score}")


def test_alert_hysteresis_and_dedup(store, risk_cfg):
    am = AlertManager(store, risk_cfg)
    seq = [(72, "raised"), (75, None), (65, None), (59, "cleared"), (71, None)]
    for i, (score, expected) in enumerate(seq):
        out = am.evaluate(T0 + timedelta(minutes=i), [FAC], [_risk(score)], [])
        states = [e.status for e in out]
        assert states == ([expected] if expected else []), (score, states)


def test_alert_reraise_after_cooldown(store, risk_cfg):
    am = AlertManager(store, risk_cfg)
    am.evaluate(T0, [FAC], [_risk(80)], [])
    am.evaluate(T0 + timedelta(minutes=1), [FAC], [_risk(10)], [])
    later = T0 + timedelta(minutes=1 + risk_cfg.alerting.min_reraise_minutes + 1)
    assert [e.status for e in am.evaluate(later, [FAC], [_risk(80)], [])] == ["raised"]


def test_incident_near_facility_alert_once(store, risk_cfg):
    am = AlertManager(store, risk_cfg)
    inc = _inc("ohgo:incident:9", sevr="major")
    out1 = am.evaluate(T0, [FAC], [_risk(10)], [inc])
    out2 = am.evaluate(T0 + timedelta(minutes=1), [FAC], [_risk(10)], [inc])
    out3 = am.evaluate(T0 + timedelta(minutes=2), [FAC], [_risk(10)], [])
    assert [e.attributes["alert_type"] for e in out1] == ["incident_near_facility"]
    assert out2 == []
    assert [e.status for e in out3] == ["cleared"]
    assert out3[0].title.startswith("Cleared:")


def test_minor_incident_does_not_alert(store, risk_cfg):
    am = AlertManager(store, risk_cfg)
    assert am.evaluate(T0, [FAC], [_risk(10)], [_inc("x", sevr="minor")]) == []
