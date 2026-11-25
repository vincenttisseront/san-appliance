#!/usr/bin/python3
"""Banc fio en VM. Un réglage n'est gardé que s'il bat la base sans régression."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUEST = Path(__file__).resolve().parent / "qemu"
sys.path.insert(0, str(REPO / "live24" / "tui"))
from san_tui.perf import assert_kept_safe, render_table, select_keep  # noqa: E402


def load_iscsi():
    spec = importlib.util.spec_from_file_location("iscsi_test", Path(__file__).resolve().parent / "iscsi-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    iscsi = load_iscsi()
    iso = Path(os.environ.get("SAN_ISO", REPO / "dist" / "san-appliance-live-24.04-amd64.iso"))
    iscsi.require(iso.is_file(), "ISO absente")
    iscsi.require(Path("/dev/kvm").exists(), "/dev/kvm absent")
    work = Path("/var/tmp/san-perf-test")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    export = work / "export.raw"
    other = work / "other.raw"
    conf = work / "conf.raw"
    export.write_bytes(b"\0" * (256 * 1024 * 1024))
    other.write_bytes(b"\0" * (32 * 1024 * 1024))
    conf.write_bytes(b"\0" * (32 * 1024 * 1024))
    subprocess.run(["mkfs.ext4", "-F", "-L", "SANCONF", "-q", str(conf)], check=True, stdout=subprocess.DEVNULL)
    base = [
        "qemu-system-x86_64", "-machine", "pc,accel=kvm:tcg", "-cpu", "max",
        "-m", "1536", "-smp", "2", "-display", "none", "-cdrom", str(iso),
    ]
    app_args = base + [
        "-netdev", "user,id=admin,restrict=on,net=192.168.10.0/24",
        "-device", "virtio-net-pci,netdev=admin,mac=52:54:00:aa:00:01,addr=0x3",
        "-netdev", "socket,id=stor,listen=127.0.0.1:4730",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:aa:00:02,addr=0x4",
        "-serial", "tcp:127.0.0.1:4731,server=on,wait=off",
    ] + iscsi.disk_args([("export01", export), ("other01", other), ("conf01", conf)])
    init_args = base + [
        "-netdev", "socket,id=stor,connect=127.0.0.1:4730",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:bb:00:02",
        "-serial", "tcp:127.0.0.1:4732,server=on,wait=off",
    ]
    report = {"steps": [], "ok": False}
    procs = []
    handles = []
    try:
        proc, handle = iscsi.start(app_args, work / "appliance.err")
        procs.append(proc)
        handles.append(handle)
        app = iscsi.Serial(4731, work / "appliance-serial.log")
        iscsi.login_shell(app)
        init_proc, init_handle = iscsi.start(init_args, work / "initiator.err")
        procs.append(init_proc)
        handles.append(init_handle)
        init = iscsi.Serial(4732, work / "initiator-serial.log")
        iscsi.login_shell(init)
        app.upload(GUEST / "appliance-setup.py", "/tmp/appliance-setup.py")
        app.upload(GUEST / "perf-knob.py", "/tmp/perf-knob.py")
        init.upload(GUEST / "perf-fio.py", "/tmp/perf-fio.py")
        app.san_result("sudo -n python3 /tmp/appliance-setup.py", 180)
        probe = app.san_result("sudo -n python3 /tmp/perf-knob.py probe", 60)
        sched_text = probe.get("scheduler") or ""
        active = sched_text.split("[", 1)[1].split("]", 1)[0] if "[" in sched_text else "none"
        trials = {}

        def reset():
            app.san_result("sudo -n python3 /tmp/perf-knob.py scheduler {}".format(active), 30)
            app.san_result("sudo -n python3 /tmp/perf-knob.py mtu 1500", 30)

        reset()
        baseline = init.san_result("sudo -n python3 /tmp/perf-fio.py 1500", 180)
        trials["baseline"] = {"workloads": baseline["workloads"], "change": {}}
        report["steps"].append("baseline: OK")

        def bench(name, knob, mtu="1500"):
            reset()
            if knob:
                app.san_result("sudo -n python3 /tmp/perf-knob.py {}".format(knob), 60)
            result = init.san_result("sudo -n python3 /tmp/perf-fio.py {}".format(mtu), 180)
            change = {}
            if name == "scheduler-none":
                change = {"scheduler": "none"}
            elif name == "scheduler-mq":
                change = {"scheduler": "mq-deadline"}
            elif name == "mtu-9000":
                change = {"mtu": 9000}
            elif name == "burst-64k":
                change = {"burst": "64k"}
            elif name == "digest-crc":
                change = {"digest": "crc32c"}
            trials[name] = {"workloads": result["workloads"], "change": change}

        for name, knob in (
            ("scheduler-none", "scheduler none"),
            ("scheduler-mq", "scheduler mq-deadline"),
        ):
            try:
                bench(name, knob)
                report["steps"].append("{}: OK".format(name))
            except Exception as exc:
                report["steps"].append("{}: SKIP {}".format(name, exc))
        try:
            bench("mtu-9000", "mtu 9000", mtu="9000")
            app.san_result("sudo -n python3 /tmp/perf-knob.py mtu 1500", 30)
            report["steps"].append("mtu-9000: OK")
        except Exception as exc:
            report["steps"].append("mtu-9000: SKIP {}".format(exc))
        try:
            bench("burst-64k", "burst")
            report["steps"].append("burst-64k: OK")
        except Exception as exc:
            report["steps"].append("burst-64k: SKIP {}".format(exc))
        checksum = (probe.get("features") or {}).get("tx-checksumming", "")
        if checksum.startswith("on"):
            try:
                bench("digest-crc", "digest")
                app.san_result("sudo -n python3 /tmp/perf-knob.py restore-digest", 30)
                report["steps"].append("digest-crc: OK")
            except Exception as exc:
                report["steps"].append("digest-crc: SKIP {}".format(exc))
        else:
            report["steps"].append("digest-crc: SKIP checksum virtuel non actif")
        kept = select_keep(trials["baseline"], trials)
        assert_kept_safe(trials["baseline"], trials, kept)
        document = {
            "status": "measured",
            "keep": kept,
            "current": "baseline",
            "trials": trials,
            "probes": {
                "io_uring": probe.get("io_uring"),
                "governor": probe.get("governor"),
                "write_cache": probe.get("write_cache"),
                "tx_checksumming": checksum,
                "tcp_segmentation_offload": (probe.get("features") or {}).get("tcp-segmentation-offload"),
                "generic_receive_offload": (probe.get("features") or {}).get("generic-receive-offload"),
                "scheduler": probe.get("scheduler"),
                "encryption": "hors du premier banc",
                "hugepages": "non activées : pas de gain mesuré dans ce banc",
                "note": "Le cache d'écriture du disque n'a pas été forcé. io_uring n'est appliqué que si rtslib l'expose.",
            },
        }
        share = REPO / "live" / "rootfs" / "usr" / "share" / "san"
        share.mkdir(parents=True, exist_ok=True)
        (share / "perf-results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (share / "perf-results.txt").write_text(render_table(document), encoding="utf-8")
        (work / "perf-results.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        report["keep"] = kept
        report["ok"] = True
        report["steps"].append("table: OK")
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        for proc in procs:
            iscsi.stop(proc)
        for handle in handles:
            handle.close()
        (work / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"ok": report.get("ok"), "steps": report.get("steps"), "keep": report.get("keep"), "error": report.get("error")}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("perf-test: {}".format(exc), file=sys.stderr)
        sys.exit(1)
