#!/usr/bin/env python3
"""
Generate encrypted NVS partition + NVS-keys partition for each device listed
in a CSV file.  Uses esp-idf-nvs-partition-gen under the hood.

Usage
-----
    python scripts/generate_nvs.py devices.csv -o output/

Input CSV format (header required):
    dev_num,competition_id,card_secret,hmac_len,webhook_secret[,wifi_ssid,wifi_pass,ingest_url]
    1,1,my_card_secret,12,my_webhook_secret,MySSID,MyPass,http://10.0.0.1:5001/api/ingest
    2,1,my_card_secret,12,my_webhook_secret,MySSID,MyPass,http://10.0.0.1:5001/api/ingest

Output (per row):
    output/dev<dev_num>_nvs_enc.bin   — encrypted NVS partition  (flash at sec_nvs offset, default 0xD000)
    output/dev<dev_num>_keys.bin      — NVS keys partition       (flash at nvs_keys offset, default 0xC000)

Flashing (esptool):
    esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash \\
        0xC000 output/dev1_keys.bin \\
        0xD000 output/dev1_nvs_enc.bin \\
        0x10000 firmware.bin

    Partition offsets (from ESP_TEST/partitions.csv):
        nvs_keys   0xC000  0x1000
        sec_nvs    0xD000  0x3000
        app0       0x10000

    The firmware binary is the same for all devices.
    Only the keys + encrypted NVS change per device.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile


NVS_NAMESPACE = "config"
DEFAULT_PARTITION_SIZE = 0x3000  # must match sec_nvs in partitions.csv


def generate_device(
    *,
    dev_num: int,
    competition_id: int,
    card_secret: str,
    hmac_len: int,
    webhook_secret: str,
    wifi_ssid: str = "",
    wifi_pass: str = "",
    ingest_url: str = "",
    outdir: str,
    partition_size: int = DEFAULT_PARTITION_SIZE,
) -> tuple[str, str]:
    """Generate encrypted NVS + keys for one device. Returns (nvs_path, keys_path)."""

    rows = [
        ["key", "type", "encoding", "value"],
        [NVS_NAMESPACE, "namespace", "", ""],
        ["dev_num",        "data", "i32",    str(dev_num)],
        ["competition_id", "data", "i32",    str(competition_id)],
        ["card_secret",    "data", "string", card_secret],
        ["hmac_len",       "data", "i32",    str(hmac_len)],
        ["webhook_secret", "data", "string", webhook_secret],
    ]
    if wifi_ssid:
        rows.append(["wifi_ssid",  "data", "string", wifi_ssid])
    if wifi_pass:
        rows.append(["wifi_pass",  "data", "string", wifi_pass])
    if ingest_url:
        rows.append(["ingest_url", "data", "string", ingest_url])

    nvs_out = os.path.join(outdir, f"dev{dev_num}_nvs_enc.bin")
    keys_out = os.path.join(outdir, f"dev{dev_num}_keys.bin")

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "config.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        # Run with cwd=tmpdir so the tool writes keys/ relative to it
        subprocess.check_call(
            [
                sys.executable,
                "-m", "esp_idf_nvs_partition_gen",
                "encrypt",
                "config.csv",
                "nvs_enc.bin",
                str(partition_size),
                "--keygen",
                "--keyfile", "keys.bin",
            ],
            cwd=tmpdir,
            timeout=30,
            stdout=subprocess.DEVNULL,
        )

        # Copy outputs to final location
        with open(os.path.join(tmpdir, "nvs_enc.bin"), "rb") as src:
            with open(nvs_out, "wb") as dst:
                dst.write(src.read())
        with open(os.path.join(tmpdir, "keys", "keys.bin"), "rb") as src:
            with open(keys_out, "wb") as dst:
                dst.write(src.read())

    return nvs_out, keys_out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate encrypted NVS + keys partitions for ESP32 devices.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csv_file", help="CSV with columns: dev_num, competition_id, card_secret, hmac_len, webhook_secret")
    parser.add_argument("-o", "--outdir", default="output", help="Output directory (default: output/)")
    parser.add_argument("--size", default=hex(DEFAULT_PARTITION_SIZE), help=f"NVS partition size (default: {hex(DEFAULT_PARTITION_SIZE)})")
    args = parser.parse_args()

    partition_size = int(args.size, 0)
    os.makedirs(args.outdir, exist_ok=True)

    with open(args.csv_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"dev_num", "competition_id", "card_secret", "hmac_len", "webhook_secret"}
        if not required.issubset(set(reader.fieldnames or [])):
            sys.exit(f"CSV must have columns: {', '.join(sorted(required))}")

        for row in reader:
            dev_num = int(row["dev_num"])
            nvs_path, keys_path = generate_device(
                dev_num=dev_num,
                competition_id=int(row["competition_id"]),
                card_secret=row["card_secret"],
                hmac_len=int(row["hmac_len"]),
                webhook_secret=row["webhook_secret"],
                wifi_ssid=row.get("wifi_ssid", ""),
                wifi_pass=row.get("wifi_pass", ""),
                ingest_url=row.get("ingest_url", ""),
                outdir=args.outdir,
                partition_size=partition_size,
            )
            print(f"dev {dev_num:>3d}:  {nvs_path}  {keys_path}")

    print(f"\nDone. Flash each device with:")
    print(f"  esptool.py --port /dev/ttyUSB0 --baud 460800 write_flash \\")
    print(f"      0xC000 {args.outdir}/dev<N>_keys.bin \\")
    print(f"      0xD000 {args.outdir}/dev<N>_nvs_enc.bin \\")
    print(f"      0x10000 firmware.bin")


if __name__ == "__main__":
    main()
