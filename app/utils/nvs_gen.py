# app/utils/nvs_gen.py
"""
Generate ESP32 NVS partition binaries (plain or encrypted) in-memory.

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
from typing import NamedTuple


NVS_NAMESPACE = "config"


class EncryptedNVS(NamedTuple):
    """Pair of binaries produced by encrypted NVS generation."""
    nvs_bin: bytes   # encrypted NVS partition (flash at sec_nvs offset)
    keys_bin: bytes  # NVS keys partition, 0x1000 bytes (flash at nvs_keys offset)


def _build_csv_rows(
    *,
    dev_num: int,
    competition_id: int,
    card_secret: str,
    hmac_len: int,
    webhook_secret: str,
    wifi_ssid: str = "",
    wifi_pass: str = "",
    ingest_url: str = "",
) -> list[list[str]]:
    """Return CSV rows for the NVS partition (shared by plain and encrypted)."""
    rows = [
        ["key", "type", "encoding", "value"],
        [NVS_NAMESPACE, "namespace", "", ""],
        ["dev_num",        "data", "i32",    str(dev_num)],
        ["competition_id", "data", "i32",    str(competition_id)],
        ["card_secret",    "data", "string", card_secret],
        ["hmac_len",       "data", "i32",    str(hmac_len)],
        ["webhook_secret", "data", "string", webhook_secret],
    ]
    # WiFi / receiver fields — only written when non-empty to save NVS space
    if wifi_ssid:
        rows.append(["wifi_ssid",  "data", "string", wifi_ssid])
    if wifi_pass:
        rows.append(["wifi_pass",  "data", "string", wifi_pass])
    if ingest_url:
        rows.append(["ingest_url", "data", "string", ingest_url])
    return rows


def generate_nvs_partition(
    *,
    dev_num: int,
    competition_id: int,
    card_secret: str,
    hmac_len: int,
    webhook_secret: str,
    wifi_ssid: str = "",
    wifi_pass: str = "",
    ingest_url: str = "",
    partition_size: int = 0x6000,
) -> bytes:
    """
    Return a raw **plaintext** NVS partition binary.

    partition_size must be a multiple of 4096 (one NVS page).
    Default 0x6000 = 24 576 bytes = 6 pages.
    """
    if partition_size % 4096 != 0 or partition_size < 4096:
        raise ValueError(f"partition_size must be a multiple of 4096, got {partition_size:#x}")

    rows = _build_csv_rows(
        dev_num=dev_num,
        competition_id=competition_id,
        card_secret=card_secret,
        hmac_len=hmac_len,
        webhook_secret=webhook_secret,
        wifi_ssid=wifi_ssid,
        wifi_pass=wifi_pass,
        ingest_url=ingest_url,
    )

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


def generate_encrypted_nvs_partition(
    *,
    dev_num: int,
    competition_id: int,
    card_secret: str,
    hmac_len: int,
    webhook_secret: str,
    wifi_ssid: str = "",
    wifi_pass: str = "",
    ingest_url: str = "",
    partition_size: int = 0x3000,
) -> EncryptedNVS:
    """
    Return an :class:`EncryptedNVS` containing:

    * ``nvs_bin``  — AES-XTS-256 encrypted NVS partition (flash at *sec_nvs* offset)
    * ``keys_bin`` — 0x1000-byte NVS keys partition (flash at *nvs_keys* offset)

    A fresh random key pair is generated for **every call**, so each device
    receives its own unique encryption key.

    partition_size must be a multiple of 4096.
    Default 0x3000 = 12 288 bytes = 3 pages (matches the sec_nvs partition).
    """
    if partition_size % 4096 != 0 or partition_size < 4096:
        raise ValueError(f"partition_size must be a multiple of 4096, got {partition_size:#x}")

    rows = _build_csv_rows(
        dev_num=dev_num,
        competition_id=competition_id,
        card_secret=card_secret,
        hmac_len=hmac_len,
        webhook_secret=webhook_secret,
        wifi_ssid=wifi_ssid,
        wifi_pass=wifi_pass,
        ingest_url=ingest_url,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "config.csv")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

        # Run with cwd=tmpdir and relative paths — the tool mutates outdir
        # when the output path is absolute, which breaks keyfile placement.
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
        )

        with open(os.path.join(tmpdir, "nvs_enc.bin"), "rb") as f:
            nvs_bin = f.read()
        with open(os.path.join(tmpdir, "keys", "keys.bin"), "rb") as f:
            keys_bin = f.read()

        return EncryptedNVS(nvs_bin=nvs_bin, keys_bin=keys_bin)
