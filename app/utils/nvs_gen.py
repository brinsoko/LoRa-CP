# app/utils/nvs_gen.py
"""
Generate an ESP32 NVS partition binary in-memory.

Uses esp-idf-nvs-partition-gen (pip install esp-idf-nvs-partition-gen).
The CLI tool requires real files, so we use a TemporaryDirectory and invoke
it via subprocess — this is the most stable approach across library versions.

NVS key contract (namespace "config"):
  dev_num        i32    LoRaDevice.dev_num
  competition_id i32    session competition_id
  card_secret    string DEVICE_CARD_SECRET
  hmac_len       i32    DEVICE_CARD_HMAC_LEN
  webhook_secret string LORA_WEBHOOK_SECRET
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile


NVS_NAMESPACE = "config"


def generate_nvs_partition(
    *,
    dev_num: int,
    competition_id: int,
    card_secret: str,
    hmac_len: int,
    webhook_secret: str,
    partition_size: int = 0x6000,
) -> bytes:
    """
    Return a raw NVS partition binary suitable for flashing at nvs_offset.

    partition_size must be a multiple of 4096 (one NVS page).
    Default 0x6000 = 24 576 bytes = 6 pages.
    """
    if partition_size % 4096 != 0 or partition_size < 4096:
        raise ValueError(f"partition_size must be a multiple of 4096, got {partition_size:#x}")

    rows = [
        ["key", "type", "encoding", "value"],
        [NVS_NAMESPACE, "namespace", "", ""],
        ["dev_num",        "data", "i32",    str(dev_num)],
        ["competition_id", "data", "i32",    str(competition_id)],
        ["card_secret",    "data", "string", card_secret],
        ["hmac_len",       "data", "i32",    str(hmac_len)],
        ["webhook_secret", "data", "string", webhook_secret],
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "config.csv")
        bin_path = os.path.join(tmpdir, "nvs.bin")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        subprocess.check_call(
            [
                sys.executable,
                "-m", "esp_idf_nvs_partition_gen",
                "generate",
                csv_path,
                bin_path,
                str(partition_size),
            ],
            timeout=30,
        )

        with open(bin_path, "rb") as f:
            return f.read()
