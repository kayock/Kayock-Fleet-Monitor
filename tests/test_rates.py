"""Delta math for network and disk throughput — the acceptance criterion
'latency of counters over elapsed time' lives here."""
from sampler import (compute_disk_io_rates, compute_interface_rates,
                     is_loopback_name, normalize_disk_counters,
                     normalize_net_counters, valid_elapsed)


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------
def test_net_rates_basic_two_seconds():
    prev = {"Ethernet": (100, 200)}
    cur = {"Ethernet": (1100, 1200)}
    r = compute_interface_rates(prev, cur, 2.0)
    assert r["per_nic"]["Ethernet"]["sent_bps"] == 500.0
    assert r["per_nic"]["Ethernet"]["recv_bps"] == 500.0
    assert r["totals"]["sent_bps"] == 500.0
    assert r["totals"]["recv_bps"] == 500.0


def test_net_first_observation_is_none_not_zero():
    r = compute_interface_rates({}, {"Ethernet": (1000, 2000)}, 2.0)
    assert r["per_nic"]["Ethernet"]["sent_bps"] is None
    assert r["per_nic"]["Ethernet"]["recv_bps"] is None
    assert r["totals"]["sent_bps"] is None  # totals honest, not a fake 0


def test_net_zero_elapsed_invalid():
    prev = {"A": (0, 0)}
    r = compute_interface_rates(prev, {"A": (100, 100)}, 0.0)
    assert r["per_nic"]["A"]["sent_bps"] is None


def test_net_first_sample_elapsed_none():
    r = compute_interface_rates({"A": (0, 0)}, {"A": (10, 10)}, None)
    assert r["per_nic"]["A"]["sent_bps"] is None


def test_net_negative_delta_counter_reset_clamps_to_zero():
    prev = {"A": (10_000, 20_000)}
    cur = {"A": (5_000, 25_000)}  # sent counter went backwards (reset)
    r = compute_interface_rates(prev, cur, 1.0)
    assert r["per_nic"]["A"]["sent_bps"] == 0.0
    assert r["per_nic"]["A"]["recv_bps"] == 5000.0


def test_net_totals_exclude_loopback():
    prev = {
        "Ethernet": (100, 100),
        "Loopback Pseudo-Interface 1": (50, 50),
    }
    cur = {
        "Ethernet": (300, 500),
        "Loopback Pseudo-Interface 1": (150, 250),
    }
    r = compute_interface_rates(prev, cur, 1.0,
                                skip_names={"Loopback Pseudo-Interface 1"})
    assert r["totals"]["sent_bps"] == 200.0
    assert r["totals"]["recv_bps"] == 400.0
    assert r["per_nic"]["Loopback Pseudo-Interface 1"]["sent_bps"] == 100.0


def test_net_new_adapter_midrun_and_vanished_adapter():
    prev = {"A": (0, 0)}
    cur = {"A": (100, 100), "B": (10_000, 10_000)}  # B just appeared
    r = compute_interface_rates(prev, cur, 2.0)
    assert r["per_nic"]["B"]["sent_bps"] is None  # never a bogus 5000 B/s
    assert r["totals"]["sent_bps"] == 50.0        # only A counts
    r2 = compute_interface_rates({"A": (0, 0), "B": (999, 999)},
                                 {"A": (100, 100)}, 1.0)  # B vanished
    assert "B" not in r2["per_nic"]
    assert r2["totals"]["sent_bps"] == 100.0


def test_is_loopback_name():
    assert is_loopback_name("lo")
    assert is_loopback_name("Loopback Pseudo-Interface 1")
    assert not is_loopback_name("Ethernet 2")
    assert not is_loopback_name("Wi-Fi")


def test_normalize_net_counters_tolerates_missing_fields():
    class C:
        bytes_sent, bytes_recv = 1, 2
    assert normalize_net_counters({"x": C()}) == {"x": (1, 2)}
    assert normalize_net_counters(None) == {}


# ---------------------------------------------------------------------------
# disk
# ---------------------------------------------------------------------------
def _disk(rb, wb, rc, wc):
    return (rb, wb, rc, wc)


def test_disk_rates_basic():
    prev = {"PhysicalDrive0": _disk(1000, 2000, 10, 20)}
    cur = {"PhysicalDrive0": _disk(3000, 6000, 30, 40)}
    r = compute_disk_io_rates(prev, cur, 2.0)
    assert r["aggregate"]["read_bps"] == 1000.0
    assert r["aggregate"]["write_bps"] == 2000.0
    assert r["aggregate"]["read_iops"] == 10.0
    assert r["aggregate"]["write_iops"] == 10.0
    assert r["per_disk"]["PhysicalDrive0"]["read_bps"] == 1000.0


def test_disk_aggregate_sums_multiple_physical_disks():
    prev = {"D0": _disk(0, 0, 0, 0), "D1": _disk(0, 0, 0, 0)}
    cur = {"D0": _disk(1000, 0, 5, 0), "D1": _disk(0, 2000, 0, 10)}
    r = compute_disk_io_rates(prev, cur, 1.0)
    assert r["aggregate"]["read_bps"] == 1000.0
    assert r["aggregate"]["write_bps"] == 2000.0
    assert r["aggregate"]["read_iops"] == 5.0
    assert r["aggregate"]["write_iops"] == 10.0


def test_disk_new_disk_excluded_from_aggregate():
    prev = {"D0": _disk(0, 0, 0, 0)}
    cur = {"D0": _disk(100, 100, 1, 1), "D1": _disk(10**9, 10**9, 999, 999)}
    r = compute_disk_io_rates(prev, cur, 1.0)
    # D1 just appeared -> its giant first-seen delta must NOT pollute totals
    assert r["aggregate"]["read_bps"] == 100.0
    assert r["per_disk"]["D1"]["read_bps"] is None


def test_disk_first_and_zero_elapsed():
    r = compute_disk_io_rates({}, {"D0": _disk(1, 1, 1, 1)}, 2.0)
    assert r["aggregate"]["read_bps"] is None
    r2 = compute_disk_io_rates({"D0": _disk(1, 1, 1, 1)},
                               {"D0": _disk(2, 2, 2, 2)}, 0.0)
    assert r2["aggregate"]["read_bps"] is None
    r3 = compute_disk_io_rates({"D0": _disk(1, 1, 1, 1)}, {}, 2.0)
    assert r3["aggregate"]["read_bps"] is None  # nothing common -> no fake rate
    assert r3["per_disk"] == {}


def test_valid_elapsed():
    assert valid_elapsed(None) is None
    assert valid_elapsed(0) is None
    assert valid_elapsed(-1) is None
    assert valid_elapsed(2.0) == 2.0


def test_classify_volume_and_adapter_and_idle():
    from sampler import (classify_adapter, classify_volume,
                         is_idle_process_name)
    assert classify_volume("/dev/nvme0n1p2", "/", "ext4") == "physical"
    assert classify_volume("/dev/loop0", "/snap/firefox/1", "squashfs") == "snap"
    assert classify_volume("tmpfs", "/run", "tmpfs") == "virtual"
    assert classify_adapter("eno1") == "physical"
    assert classify_adapter("docker0") == "virtual"
    assert classify_adapter("br-abc123") == "virtual"
    assert classify_adapter("veth0a4d82e") == "virtual"
    assert classify_adapter("lo") == "loopback"
    assert classify_adapter("Tailscale") == "overlay"
    assert is_idle_process_name("System Idle Process")
    assert not is_idle_process_name("python.exe")
