#!/usr/bin/env python3

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "services.json"
DEFAULT_OUTPUT_PATH = BASE_DIR / "status_cache.json"
DEFAULT_HOSTS_PATH = Path.home() / "Information" / "hosts.txt"
DEFAULT_CREDENTIALS_PATH = Path.home() / "Information" / "credentials.txt"
DEFAULT_SECRET_PATH = Path.home() / "Information" / "credentials_secret.txt"
DEFAULT_API_PORT = 8001
SSH_OPTIONS = [
    "-o",
    "ConnectTimeout=5",
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "PasswordAuthentication=yes",
]

_CACHE_LOCK = threading.Lock()
_STATUS_CACHE = {
    "services": [],
    "metrics": {},
    "errors_by_host": {},
    "ip_to_name": {},
    "updated_at": None,
}


def load_services(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config.get("services", [])
    except FileNotFoundError:
        return []


def load_lab_hosts(info_hosts_path):
    hosts = []
    if not info_hosts_path.exists():
        return hosts

    current_name = None
    for raw_line in info_hosts_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            current_name = line.lstrip("#").strip()
            continue
        if current_name:
            hosts.append({"name": current_name, "ip": line})
            current_name = None
        else:
            hosts.append({"name": line, "ip": line})
    return hosts


def load_ssh_credentials(credentials_path):
    creds = {}
    if not credentials_path.exists():
        return creds
    for raw_line in credentials_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "@" in line:
            user, host = line.split("@", 1)
            creds[host] = user
    return creds


def load_ssh_password(secret_path):
    if not secret_path.exists():
        return None
    return secret_path.read_text(encoding="utf-8").strip()


def build_ip_to_name_map(lab_hosts):
    mapping = {"localhost": "Murkrow", "127.0.0.1": "Murkrow"}
    for host in lab_hosts:
        mapping[host["ip"]] = host["name"].split(" ")[0]
    return mapping


def resolve_ssh_target(target, user, credentials):
    if user:
        return f"{user}@{target}"
    host_user = credentials.get(target)
    if host_user:
        return f"{host_user}@{target}"
    return target


def run_command(args, timeout=2):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", "missing"


def run_shell_command(cmd, timeout=5):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except FileNotFoundError:
        return 1, "", "missing"


def run_remote_command(target, command, user, credentials, secret_path, timeout=5):
    ssh_target = resolve_ssh_target(target, user, credentials)
    password = load_ssh_password(secret_path)
    if password:
        cmd = f"sshpass -p '{password}' ssh {' '.join(SSH_OPTIONS)} {ssh_target} {command}"
        return run_shell_command(cmd, timeout=timeout)
    return run_command(["ssh", *SSH_OPTIONS, ssh_target, command], timeout=timeout)


def ping_host(target):
    code, _, _ = run_command(["ping", "-c", "1", "-W", "1", target])
    return code == 0


def tcp_check(target, port):
    try:
        with socket.create_connection((target, port), timeout=2):
            return True
    except Exception:
        return False


def process_check(target):
    code, stdout, _ = run_command(["pgrep", "-x", target])
    return code == 0 and bool(stdout)


def service_check(target):
    code, stdout, _ = run_command(["systemctl", "is-active", target])
    return code == 0 and stdout == "active"


def ssh_login_check(target, command, user, credentials, secret_path):
    code, stdout, stderr = run_remote_command(target, command, user, credentials, secret_path, timeout=5)
    return code == 0, stdout or stderr


def ssh_command_check(target, command, user, credentials, secret_path):
    if not command:
        return False, "no command specified"
    code, stdout, stderr = run_remote_command(target, command, user, credentials, secret_path, timeout=10)
    return code == 0, stdout or stderr or f"exit {code}"


def docker_check(target, container, user, credentials, secret_path):
    if not container:
        return False, "no container specified"
    cmd = (
        f"docker inspect --format '{{{{.State.Running}}}}' {container} 2>/dev/null "
        f"|| docker ps --filter 'name={container}' --format '{{{{.Names}}}}|{{{{.Status}}}}'"
    )
    if target in (None, "localhost", "127.0.0.1"):
        code, stdout, stderr = run_shell_command(cmd, timeout=10)
    else:
        code, stdout, stderr = run_remote_command(target, cmd, user, credentials, secret_path, timeout=10)
    healthy = False
    if code == 0:
        result = stdout.strip().lower()
        healthy = result == "true" or bool(result)
    return healthy, stdout or stderr or f"exit {code}"


def dmesg_check(target, user, credentials, secret_path):
    cmd = "dmesg | tail -n 120 | grep -iE 'error|fail|panic|warn' | head -n 20"
    if target in (None, "localhost", "127.0.0.1"):
        code, stdout, stderr = run_shell_command(cmd, timeout=10)
    else:
        code, stdout, stderr = run_remote_command(target, cmd, user, credentials, secret_path, timeout=10)
    if code == 0 and stdout:
        errors = [line.strip() for line in stdout.split("\n") if line.strip()]
        return False, {"errors": errors, "count": len(errors)}
    if code == 1:
        return True, {"errors": [], "count": 0}
    return False, {"errors": [stderr or f"exit {code}"], "count": 1}


def parse_sensor_temperatures(output):
    temps = []
    for line in output.splitlines():
        match = re.search(r"temp\d+_input:\s*([0-9]+\.?[0-9]*)", line)
        if match:
            try:
                temps.append(float(match.group(1)))
            except ValueError:
                continue
    return temps


def temperature_check(target, threshold=None, user=None, credentials=None, secret_path=None):
    sensor_cmd = "sensors -u 2>/dev/null"
    if target in (None, "localhost", "127.0.0.1"):
        code, stdout, stderr = run_shell_command(sensor_cmd, timeout=10)
    else:
        code, stdout, stderr = run_remote_command(target, sensor_cmd, user, credentials, secret_path, timeout=10)

    temperatures = parse_sensor_temperatures(stdout) if stdout else []
    source = "sensors"
    if not temperatures:
        sys_cmd = "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null"
        if target in (None, "localhost", "127.0.0.1"):
            code, stdout, stderr = run_shell_command(sys_cmd, timeout=10)
        else:
            code, stdout, stderr = run_remote_command(target, sys_cmd, user, credentials, secret_path, timeout=10)
        for line in stdout.splitlines():
            try:
                temp = float(line.strip()) / 1000.0
                temperatures.append(temp)
            except ValueError:
                continue
        source = "thermal_zone"

    if not temperatures:
        return "offline", {"error": stderr or "no temperature sensors found"}

    max_temp = round(max(temperatures), 1)
    status = "healthy"
    if threshold is not None:
        try:
            threshold_value = float(threshold)
            critical_threshold = threshold_value + 15
            if max_temp >= critical_threshold:
                status = "critical"
            elif max_temp > threshold_value:
                status = "warning"
        except (TypeError, ValueError):
            threshold_value = None
    elif max_temp > 85:
        status = "critical"
    elif max_temp > 75:
        status = "warning"

    return status, {"max": max_temp, "source": source, "threshold": threshold, "count": len(temperatures)}


def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as f:
            return int(float(f.readline().split()[0]))
    except Exception:
        return None


def format_uptime(seconds):
    if seconds is None:
        return "unknown"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def read_temperatures():
    temps = []
    thermal_dir = Path("/sys/class/thermal")
    if thermal_dir.exists():
        for zone in sorted(thermal_dir.glob("thermal_zone*")):
            try:
                label = zone.joinpath("type").read_text().strip()
                raw = zone.joinpath("temp").read_text().strip()
                temperature = int(raw) / 1000.0
                temps.append({"label": label, "celsius": round(temperature, 1)})
            except Exception:
                continue
    if not temps:
        code, stdout, _ = run_command(["sensors"], timeout=2)
        if code == 0 and stdout:
            for line in stdout.splitlines():
                if "+" in line and "°C" in line:
                    parts = line.split()
                    label = parts[0].strip(":")
                    for piece in parts:
                        if piece.startswith("+") and piece.endswith("°C"):
                            try:
                                c = float(piece.strip("+°C"))
                                temps.append({"label": label, "celsius": round(c, 1)})
                                break
                            except ValueError:
                                continue
    return temps


def build_status(config_path, hosts_path, credentials_path, secret_path):
    services = []
    errors_by_host = {}
    lab_hosts = load_lab_hosts(hosts_path)
    ip_to_name = build_ip_to_name_map(lab_hosts)
    configured_services = load_services(config_path)
    credentials = load_ssh_credentials(credentials_path)
    for svc in configured_services:
        name = svc.get("name", "unknown")
        kind = svc.get("type", "ping")
        target = svc.get("target")
        port = svc.get("port")
        description = svc.get("description", "")
        details = ""
        healthy = False
        msg = ""
        if kind == "ping" and target:
            healthy = ping_host(target)
            details = f"ping {target}"
        elif kind == "tcp" and target and port:
            healthy = tcp_check(target, port)
            details = f"{target}:{port}"
        elif kind == "process" and target:
            healthy = process_check(target)
            details = f"process {target}"
        elif kind == "service" and target:
            healthy = service_check(target)
            details = f"system service {target}"
        elif kind in ("ssh", "ssh_access") and target:
            healthy, msg = ssh_login_check(target, "echo ssh-ok", svc.get("user"), credentials, secret_path)
            details = f"ssh access to {target}"
        elif kind == "ssh_cmd" and target:
            healthy, msg = ssh_command_check(target, svc.get("command"), svc.get("user"), credentials, secret_path)
            details = f"ssh command on {target}: {svc.get('command')}"
        elif kind == "docker" and target:
            healthy, msg = docker_check(target, svc.get("container"), svc.get("user"), credentials, secret_path)
            details = f"docker container {svc.get('container')} on {target}"
        elif kind == "dmesg":
            healthy, msg = dmesg_check(target, svc.get("user"), credentials, secret_path)
            details = f"dmesg health on {target or 'localhost'}"
            if isinstance(msg, dict) and msg.get("errors"):
                host_key = target or "localhost"
                errors_by_host.setdefault(host_key, []).extend(msg["errors"])
                msg = f"{msg['count']} issues found"
        elif kind == "temperature":
            status_str, msg = temperature_check(target, svc.get("threshold"), svc.get("user"), credentials, secret_path)
            healthy = status_str == "healthy"
            details = f"temperature on {target or 'localhost'}"
            if isinstance(msg, dict):
                if msg.get("error"):
                    details = msg["error"]
                else:
                    details = f"{msg['source']}: {msg['max']}°C"
                    if msg.get("threshold") is not None:
                        details += f" (threshold {msg['threshold']}°C)"
            msg = status_str
        else:
            details = "invalid config"

        if not healthy and msg and kind != "temperature":
            details = f"{details}: {msg}" if details else msg

        host_name = ip_to_name.get(target, target or "localhost")
        status_label = msg if kind == "temperature" else ("online" if healthy else "offline")
        if kind == "dmesg":
            status_label = "healthy" if healthy else "warning"

        services.append({
            "name": name,
            "type": kind,
            "target": target,
            "host_name": host_name,
            "port": port,
            "description": description,
            "status": status_label,
            "healthy": healthy,
            "details": details,
        })

    uptime_seconds = get_uptime_seconds()
    metrics = {
        "uptime": format_uptime(uptime_seconds),
        "uptime_seconds": uptime_seconds,
        "temperatures": read_temperatures(),
        "hosts": lab_hosts,
        "host_count": len(lab_hosts),
    }
    return {
        "services": services,
        "metrics": metrics,
        "errors_by_host": errors_by_host,
        "ip_to_name": build_ip_to_name_map(lab_hosts),
    }


def save_status(status, output_path):
    global _STATUS_CACHE
    status["updated_at"] = time.time()
    _STATUS_CACHE = status
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)


def load_cache(output_path):
    global _STATUS_CACHE
    if output_path.exists():
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                _STATUS_CACHE = json.load(f)
        except Exception as e:
            print(f"Failed to load cache: {e}")
    return _STATUS_CACHE


class StatusHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        if self.path == "/status":
            with _CACHE_LOCK:
                status = _STATUS_CACHE.copy()
            payload = json.dumps(status, indent=2)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
            return
        self.send_response(404)
        self.end_headers()


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def parse_args():
    parser = argparse.ArgumentParser(description="Standalone health checker for lab dashboard integration.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to services.json")
    parser.add_argument("--hosts", default=DEFAULT_HOSTS_PATH, help="Path to hosts metadata")
    parser.add_argument("--credentials", default=DEFAULT_CREDENTIALS_PATH, help="Path to SSH credentials file")
    parser.add_argument("--secret", default=DEFAULT_SECRET_PATH, help="Path to SSH password secret")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_PATH, help="Path to write status cache JSON")
    parser.add_argument("--interval", type=int, default=0, help="Seconds between repeated checks (0 = run once)")
    parser.add_argument("--serve", action="store_true", help="Run an HTTP API server for dashboard status requests")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help="Port for the HTTP status API")
    return parser.parse_args()


def start_api_server(port):
    server_address = ('', port)
    httpd = ThreadingHTTPServer(server_address, StatusHandler)
    print(f"HealthChecker status API available at http://localhost:{port}/status")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    args = parse_args()
    output_path = Path(args.output)
    load_cache(output_path)
    server_thread = None
    if args.serve:
        server_thread = threading.Thread(target=start_api_server, args=(args.api_port,), daemon=True)
        server_thread.start()
    if args.interval > 0:
        print(f"Starting standalone health checker, writing cache to {output_path}")
        while True:
            status = build_status(args.config, Path(args.hosts), Path(args.credentials), Path(args.secret))
            save_status(status, output_path)
            print(f"Wrote status cache at {time.strftime('%Y-%m-%d %H:%M:%S')} to {output_path}")
            time.sleep(args.interval)
    else:
        status = build_status(args.config, Path(args.hosts), Path(args.credentials), Path(args.secret))
        save_status(status, output_path)
        print(f"Wrote status cache to {output_path}")
        if args.serve:
            print("Serving status API. Press Ctrl+C to exit.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
