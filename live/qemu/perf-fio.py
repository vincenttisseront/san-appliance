#!/usr/bin/python3
"""fio sur le LUN iSCSI. Les chiffres sont ceux du banc, pas une estimation."""

import json
import pathlib
import subprocess
import sys
import time

TARGET = "iqn.2026-11.local.san:disk"
PORTAL = "10.1.0.10"
ALLOWED = "iqn.2026-11.local.client:host"
CHAP_USER = "client1"
SECRET = "Init1SecretOK1"
NIC_MAC = "52:54:00:bb:00:02"
LOADS = (
    ("read", "read", "1M"),
    ("write", "write", "1M"),
    ("randread-4k", "randread", "4k"),
    ("randwrite-4k", "randwrite", "4k"),
    ("randread-64k", "randread", "64k"),
    ("randwrite-64k", "randwrite", "64k"),
    ("randread-1m", "randread", "1M"),
    ("randwrite-1m", "randwrite", "1M"),
)


def run(args, check=True):
    proc = subprocess.run(args, text=True, capture_output=True)
    if check and proc.returncode != 0:
        sys.stderr.write((proc.stderr or proc.stdout or "")[-500:])
        raise SystemExit("commande en échec : {}".format(" ".join(args[:4])))
    return proc


def prepare():
    for entry in pathlib.Path("/sys/class/net").iterdir():
        if entry.name == "lo":
            continue
        mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
        if mac == NIC_MAC:
            run(["ip", "link", "set", entry.name, "up"])
            mtu = sys.argv[1] if len(sys.argv) > 1 else "1500"
            run(["ip", "link", "set", entry.name, "mtu", mtu])
            run(["ip", "addr", "replace", "10.1.0.20/24", "dev", entry.name])
            return
    raise SystemExit("interface initiateur absente")


def login():
    path = pathlib.Path("/etc/iscsi/initiatorname.iscsi")
    path.write_text("InitiatorName={}\n".format(ALLOWED), encoding="utf-8")
    run(["systemctl", "restart", "iscsid"])
    time.sleep(1)
    run(["iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--logout"], check=False)
    run(["iscsiadm", "-m", "session", "--logout"], check=False)
    run(["iscsiadm", "-m", "node", "-o", "new", "-T", TARGET, "-p", PORTAL], check=False)
    base = ["iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--op", "update"]
    run(base + ["-n", "node.startup", "-v", "manual"])
    run(base + ["-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(base + ["-n", "node.session.auth.username", "-v", CHAP_USER])
    run(base + ["-n", "node.session.auth.password", "-v", SECRET])
    deadline = time.time() + 60
    last = None
    while time.time() < deadline:
        last = run(["iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--login"], check=False)
        err = (last.stderr or "") + (last.stdout or "")
        if last.returncode == 0 or "already present" in err:
            return
        time.sleep(2)
    raise SystemExit("login iSCSI refusé : {}".format((last.stderr if last else "")[-300:]))


def device():
    root = pathlib.Path("/dev/disk/by-path")
    deadline = time.time() + 20
    while time.time() < deadline:
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if "iscsi-{}".format(TARGET) in entry.name and entry.name.endswith("-lun-0"):
                    return str(entry)
        time.sleep(1)
    raise SystemExit("LUN absente")


def one(name, rw, bs, dev):
    proc = run([
        "fio", "--name", name, "--filename", dev, "--rw", rw, "--bs", bs,
        "--direct=1", "--ioengine=libaio", "--iodepth=8",
        "--runtime=2", "--time_based=1", "--size=64M",
        "--lat_percentiles=1", "--percentile_list=99",
        "--group_reporting", "--output-format=json",
    ], check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr[-400:])
    job = json.loads(proc.stdout)["jobs"][0]
    side = job["read"] if rw.startswith("read") or rw == "randread" else job["write"]
    if rw in {"read", "randread"}:
        side = job["read"]
    else:
        side = job["write"]
    io_bytes = side.get("io_bytes") or 0
    cpu = float(job.get("usr_cpu") or 0) + float(job.get("sys_cpu") or 0)
    gb = io_bytes / 1e9
    pct = side.get("clat_ns", {}).get("percentile", {})
    p99 = pct.get("99.000000", pct.get("99.0"))
    if p99 is None:
        raise SystemExit("p99 absent pour {}".format(name))
    return {
        "iops": side.get("iops") or 0,
        "p99_us": float(p99) / 1000.0,
        "throughput_bps": side.get("bw_bytes") or 0,
        "cpu_per_gb": (cpu / gb) if gb else 0,
    }


def main():
    prepare()
    login()
    dev = device()
    workloads = {}
    for name, rw, bs in LOADS:
        workloads[name] = one(name, rw, bs, dev)
    print("SAN-RESULT " + json.dumps({"workloads": workloads}, ensure_ascii=False))


if __name__ == "__main__":
    main()
