"""SDR load balancer: placement rules + actuator rendering (no hardware, no database)."""
import nova_sdr_placement as P
import nova_sdr_actuator as A

T = lambda i, label, hz, demod, meta=None: {"id": i, "label": label, "freq_hz": hz, "demod": demod, "meta": meta or {}}
DUO = {"serial": "24051A7034", "outdir": "/home/kochj/nrsp_calls"}


def test_placement_rules():
    P.selftest()   # stable, preemption on failure, MOVE_DB/HOLD damping, slave-needs-master, demod gate


def test_dual_tuner_args_follow_tuner_order():
    # fm_capture's DT mode maps argument ORDER to tuner -- the tuner number is only a marker
    out = A.render("fm_dt", [("2", T("t", "BUR Tower", 118700000, "AM"), DUO),
                             ("1", T("h", "Ham 147", 147435000, "NFM"), DUO)])
    args = out.split("nrsp_calls ", 1)[1]
    assert args.index("Ham_147:147435000:1:NFM") < args.index("BUR_Tower:118700000:2:AM"), args


def test_single_duo_tuner_uses_master_antenna_and_unassigned_stops():
    out = A.render("fm_dt", [("2", T("t", "BUR Tower", 118700000, "AM"), DUO), ("1", None, DUO)])
    assert '"BUR_Tower:118700000:Tuner 2 50 ohm:AM"' in out, out
    assert A.render("nrsp", [("Antenna B", None, {})]) is None


def test_labels_cannot_break_systemd_or_fm_capture_args():
    out = A.render("nrsp", [("Antenna B", T("c", 'CB "19": test', 27185000, "AM"), {})])
    assert '"NRSP_LABEL=CB 19 test"' in out and '"NRSP_FREQ=27.1850"' in out, out


def test_slave_never_resets_the_duo():
    out = A.render("p25_slave", [("Tuner 1 50 ohm", T("n", "LAPD NoHo", 484962500, "P25", {"nac": "466"}), {})])
    assert "\nExecStartPre=\n" in out and '"RSP_MODE=SL"' in out, out
