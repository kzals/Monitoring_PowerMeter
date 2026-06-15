import json
import logging
import os
import signal
import struct
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from pymodbus.client import ModbusSerialClient


@dataclass
class Config:
    serial_port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: int
    timeout: float
    slave_id: int
    address_offset: int
    byte_order: str
    word_order: str
    machine_poll_interval_sec: float
    api_poll_interval_sec: float

    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket: str
    measurement: str
    device_name: str


# Only collect total power values and a few extras.
# Voltage registers are split into L-N (phase-to-neutral) and L-L (line-to-line) groups.
POWER_REGISTER_MAP = {
    "P_active_total": 3060,
    "P_reactive_total": 3068,
    "P_apparent_total": 3076,
}

EXTRA_REGISTER_MAP = {
    # L-N voltage
    "v_rn": 3028,
    "v_sn": 3030,
    "v_tn": 3032,
    # L-L voltage
    "v_rs": 3020,
    "v_st": 3022,
    "v_tr": 3024,
    "i_r_amp": 3000,
    "i_s_amp": 3002,
    "i_t_amp": 3004,
    "cospi_raw_4q": 3084,
    "frequency": 3110,
}

INFLUX_FIELDS = (
    "P_active_total",
    "P_reactive_total",
    "P_apparent_total",
    "v_rn",
    "v_sn",
    "v_tn",
    "v_rs",
    "v_st",
    "v_tr",
    "i_r_amp",
    "i_s_amp",
    "i_t_amp",
    "cospi",
    "frequency",
)


def load_dotenv_if_exists(dotenv_file: str = ".env") -> None:
    env_path = Path(dotenv_file)
    if not env_path.exists():
        return

    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def sanitize_env_key(text: str) -> str:
    normalized = []
    for ch in text:
        if ch.isalnum():
            normalized.append(ch.upper())
        else:
            normalized.append("_")
    return "".join(normalized)


def pick_env(*keys: str) -> str | None:
    for key in keys:
        val = os.getenv(key)
        if val is not None and val.strip() != "":
            return val.strip()
    return None


def load_config_from_json(config_file: str = "config.json") -> list[Config]:
    load_dotenv_if_exists(".env")

    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    influx_url = pick_env("INFLUX_URL") or "http://127.0.0.1:8086"
    influx_token = pick_env("INFLUX_TOKEN") or ""
    influx_org = pick_env("INFLUX_ORG") or ""
    influx_bucket = pick_env("INFLUX_BUCKET") or "powermeter"
    influx_measurement = pick_env("INFLUX_MEASUREMENT") or "pm2220"

    devices = []

    for device_data in data.get("devices", []):
        if not device_data.get("enabled", True):
            continue

        device_name = device_data.get("name", "pm2220-unknown")
        device_key = sanitize_env_key(device_name)

        serial_port = pick_env(f"PM2220_SERIAL_PORT_{device_key}", "PM2220_SERIAL_PORT") or device_data.get("serial_port", "COM3")
        baudrate = int(pick_env(f"PM2220_BAUDRATE_{device_key}", "PM2220_BAUDRATE") or device_data.get("baudrate", 9600))
        bytesize = int(pick_env(f"PM2220_BYTESIZE_{device_key}", "PM2220_BYTESIZE") or device_data.get("bytesize", 8))
        parity = pick_env(f"PM2220_PARITY_{device_key}", "PM2220_PARITY") or device_data.get("parity", "N")
        stopbits = int(pick_env(f"PM2220_STOPBITS_{device_key}", "PM2220_STOPBITS") or device_data.get("stopbits", 1))
        timeout_sec = float(pick_env(f"PM2220_TIMEOUT_SEC_{device_key}", "PM2220_TIMEOUT_SEC") or device_data.get("timeout_sec", 1.0))
        slave_id = int(pick_env(f"PM2220_SLAVE_ID_{device_key}", "PM2220_SLAVE_ID") or device_data.get("slave_id", 1))
        address_offset = int(pick_env(f"REGISTER_ADDRESS_OFFSET_{device_key}", "REGISTER_ADDRESS_OFFSET") or device_data.get("address_offset", -1))
        byte_order = (pick_env(f"REGISTER_BYTE_ORDER_{device_key}", "REGISTER_BYTE_ORDER") or device_data.get("byte_order", "BIG")).upper()
        word_order = (pick_env(f"REGISTER_WORD_ORDER_{device_key}", "REGISTER_WORD_ORDER") or device_data.get("word_order", "BIG")).upper()
        machine_poll_interval = float(
            pick_env(f"PM2220_MACHINE_POLL_INTERVAL_SEC_{device_key}", "PM2220_MACHINE_POLL_INTERVAL_SEC")
            or device_data.get("machine_poll_interval_sec", 5)
        )
        api_poll_interval = float(
            pick_env(f"PM2220_API_POLL_INTERVAL_SEC_{device_key}", "PM2220_API_POLL_INTERVAL_SEC")
            or device_data.get("api_poll_interval_sec", 10)
        )

        device_cfg = Config(
            serial_port=serial_port,
            baudrate=baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=timeout_sec,
            slave_id=slave_id,
            address_offset=address_offset,
            byte_order=byte_order,
            word_order=word_order,
            machine_poll_interval_sec=machine_poll_interval,
            api_poll_interval_sec=api_poll_interval,
            influx_url=influx_url,
            influx_token=influx_token,
            influx_org=influx_org,
            influx_bucket=influx_bucket,
            measurement=influx_measurement,
            device_name=device_name,
        )
        # Debug: log loaded config per device
        logging.getLogger("config_load").info(
            "Loaded device: name=%s slave_id=%d serial_port=%s address_offset=%d",
            device_name, slave_id, serial_port, address_offset
        )
        devices.append(device_cfg)

    return devices


def decode_float32_from_registers(registers: list[int], byte_order: str, word_order: str) -> float:
    if len(registers) != 2:
        raise ValueError(f"Expected 2 registers for FLOAT32, got {len(registers)}")

    words = [int(registers[0]).to_bytes(2, byteorder="big"), int(registers[1]).to_bytes(2, byteorder="big")]

    if byte_order == "LITTLE":
        words = [w[::-1] for w in words]
    elif byte_order != "BIG":
        raise ValueError("byte_order must be BIG or LITTLE")

    if word_order == "LITTLE":
        words = [words[1], words[0]]
    elif word_order != "BIG":
        raise ValueError("word_order must be BIG or LITTLE")

    raw = words[0] + words[1]
    return struct.unpack(">f", raw)[0]


def decode_4q_pf(raw_value: float) -> float:
    """Decode 4-quadrant power factor."""
    if raw_value > 1:
        return 2 - raw_value
    if raw_value < -1:
        return -2 - raw_value
    return raw_value


def read_holding_registers_compat(client: ModbusSerialClient, address: int, count: int, slave_id: int) -> object:
    """Compatibility wrapper for different pymodbus versions."""
    try:
        return client.read_holding_registers(address=address, count=count, slave=slave_id)
    except TypeError:
        return client.read_holding_registers(address=address, count=count, unit=slave_id)


def read_float32(client: ModbusSerialClient, slave_id: int, register: int, cfg: Config) -> float:
    """Read 32-bit float from two consecutive registers."""
    address = register + cfg.address_offset
    response = read_holding_registers_compat(client, address=address, count=2, slave_id=slave_id)
    if response.isError():
        raise RuntimeError(f"Modbus error reading register {register} (address={address}, slave_id={slave_id}): {response}")
    result = decode_float32_from_registers(response.registers, cfg.byte_order, cfg.word_order)
    # Debug: log raw response untuk diagnosis
    logging.getLogger("modbus_debug").debug(
        "read_float32 device=%s slave_id=%d register=%d address=%d raw_registers=%s result=%.6f",
        cfg.device_name, slave_id, register, address, response.registers, result
    )
    return result


def build_point(values: dict, cfg: Config) -> Point:
    """Build InfluxDB point with totals, voltage, current, and cospi."""
    point = Point(cfg.measurement).tag("device", cfg.device_name)
    for key in INFLUX_FIELDS:
        if key in values:
            point = point.field(key, float(values[key]))

    point = point.time(datetime.now(timezone.utc), WritePrecision.NS)
    return point


def collect_device_values(client: ModbusSerialClient, cfg: Config) -> dict:
    values = {}
    logger = logging.getLogger(f"collect.{cfg.device_name}")
    
    for field_name, reg in POWER_REGISTER_MAP.items():
        values[field_name] = read_float32(client, cfg.slave_id, reg, cfg)
        logger.debug("POWER %s=%s", field_name, values[field_name])

    for field_name, reg in EXTRA_REGISTER_MAP.items():
        values[field_name] = read_float32(client, cfg.slave_id, reg, cfg)
        logger.debug("EXTRA %s=%s", field_name, values[field_name])

    values["cospi"] = decode_4q_pf(values["cospi_raw_4q"])
    del values["cospi_raw_4q"]
    logger.debug("FINAL_COSPI=%.4f", values["cospi"])
    return values


def group_devices_by_serial_port(device_configs: list[Config]) -> dict[str, list[Config]]:
    grouped = defaultdict(list)
    for cfg in device_configs:
        grouped[cfg.serial_port].append(cfg)
    return dict(grouped)


def run_bus_collector(device_configs: list[Config], running_flag: list) -> None:
    """Run collector for one serial bus and poll all devices on that bus sequentially."""
    if not device_configs:
        return

    bus_cfg = device_configs[0]
    logger = logging.getLogger(bus_cfg.serial_port)

    for cfg in device_configs[1:]:
        if (
            cfg.baudrate != bus_cfg.baudrate
            or cfg.bytesize != bus_cfg.bytesize
            or cfg.parity != bus_cfg.parity
            or cfg.stopbits != bus_cfg.stopbits
            or cfg.timeout != bus_cfg.timeout
            or cfg.address_offset != bus_cfg.address_offset
            or cfg.byte_order != bus_cfg.byte_order
            or cfg.word_order != bus_cfg.word_order
        ):
            logger.warning(
                "Device %s has different serial/register settings; using the first device settings for this bus",
                cfg.device_name,
            )

    client = ModbusSerialClient(
        port=bus_cfg.serial_port,
        baudrate=bus_cfg.baudrate,
        bytesize=bus_cfg.bytesize,
        parity=bus_cfg.parity,
        stopbits=bus_cfg.stopbits,
        timeout=bus_cfg.timeout,
    )

    influx = InfluxDBClient(url=bus_cfg.influx_url, token=bus_cfg.influx_token, org=bus_cfg.influx_org)
    write_api = influx.write_api(write_options=SYNCHRONOUS)

    logger.info("Started bus=%s with %d device(s)", bus_cfg.serial_port, len(device_configs))

    device_state = {
        cfg.device_name: {
            "cfg": cfg,
            "latest_values": None,
            "last_success_poll": None,
            "next_machine_poll": time.monotonic(),
            "next_api_write": time.monotonic(),
        }
        for cfg in device_configs
    }

    try:
        while running_flag[0]:
            if not client.connected:
                if not client.connect():
                    logger.error("Failed to open serial port %s; retrying...", bus_cfg.serial_port)
                    time.sleep(2.0)
                    continue

            for state in device_state.values():
                cfg = state["cfg"]
                now = time.monotonic()

                if now >= state["next_machine_poll"]:
                    try:
                        state["latest_values"] = collect_device_values(client, cfg)
                        state["last_success_poll"] = now
                        latest_values = state["latest_values"]
                        logger.info(
                            "R_OK %s P=%.3f Q=%.3f S=%.3f PF=%.4f",
                            cfg.device_name,
                            latest_values.get("P_active_total"),
                            latest_values.get("P_reactive_total"),
                            latest_values.get("P_apparent_total"),
                            latest_values.get("cospi"),
                        )
                        logger.debug(
                            "Machine poll OK device=%s P_active_total=%.3f kW P_reactive_total=%.3f kVAR P_apparent_total=%.3f kVA",
                            cfg.device_name,
                            latest_values.get("P_active_total"),
                            latest_values.get("P_reactive_total"),
                            latest_values.get("P_apparent_total"),
                        )
                        logger.debug(
                            "Machine poll details device=%s V_LN=[%.2f %.2f %.2f] V_LL=[%.2f %.2f %.2f] I=[%.2f %.2f %.2f] cospi=%.4f",
                            cfg.device_name,
                            latest_values.get("v_rn"),
                            latest_values.get("v_sn"),
                            latest_values.get("v_tn"),
                            latest_values.get("v_rs"),
                            latest_values.get("v_st"),
                            latest_values.get("v_tr"),
                            latest_values.get("i_r_amp"),
                            latest_values.get("i_s_amp"),
                            latest_values.get("i_t_amp"),
                            latest_values.get("cospi"),
                        )
                    except Exception as exc:
                        # Clear stale cache so disconnected devices are not written to Influx.
                        state["latest_values"] = None
                        logger.warning("Machine poll failed device=%s: %s", cfg.device_name, exc)
                    finally:
                        state["next_machine_poll"] = now + cfg.machine_poll_interval_sec

                # Write to InfluxDB at configured API interval only when we have a successful recent poll.
                if (
                    state["latest_values"] is not None
                    and state["last_success_poll"] is not None
                    and now >= state["next_api_write"]
                ):
                    try:
                        pt = build_point(state["latest_values"], cfg)
                        write_api.write(bucket=cfg.influx_bucket, org=cfg.influx_org, record=pt)
                        logger.info(
                            "W_OK %s",
                            cfg.device_name,
                        )
                    except Exception:
                        logger.exception("Failed to write to Influx for device=%s", cfg.device_name)
                    state["next_api_write"] = now + cfg.api_poll_interval_sec

            sleep_candidates = [0.2]
            for state in device_state.values():
                sleep_candidates.append(max(0.0, state["next_machine_poll"] - time.monotonic()))
                if state["latest_values"] is not None:
                    sleep_candidates.append(max(0.0, state["next_api_write"] - time.monotonic()))
            time.sleep(max(0.05, min(sleep_candidates)))
    finally:
        try:
            client.close()
        except Exception:
            pass
        influx.close()

    logger.info("Stopped")


def main() -> int:
    try:
        device_configs = load_config_from_json("config.json")
    except Exception as exc:
        logging.error("Failed to load config.json: %s", exc)
        return 1

    # Force the process timezone so log timestamps follow WIB even when the
    # parent shell or systemd service starts with a different timezone setting.
    os.environ["TZ"] = os.getenv("TZ", "Asia/Jakarta")
    if hasattr(time, "tzset"):
        time.tzset()

    log_level = (pick_env("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname).1s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not device_configs:
        logging.error("No enabled devices found in config.json")
        return 1

    grouped_devices = group_devices_by_serial_port(device_configs)
    running = [True]

    def stop_handler(signum, frame):
        del signum, frame
        logging.info("Stop signal received, shutting down...")
        running[0] = False

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    threads = []
    for serial_port, serial_device_configs in grouped_devices.items():
        thread = threading.Thread(
            target=run_bus_collector,
            args=(serial_device_configs, running),
            daemon=False,
            name=f"collector-{serial_port}",
        )
        thread.start()
        threads.append(thread)
        time.sleep(0.5)

    logging.info("Started %d bus collector(s) for %d device(s)", len(threads), len(device_configs))

    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        running[0] = False
        for thread in threads:
            thread.join(timeout=2.0)

    logging.info("All collectors stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
