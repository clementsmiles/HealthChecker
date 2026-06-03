# HealthChecker

A standalone lab health checker that can produce a cached status file for the dashboard.

## Purpose

This project runs service and host health checks independently of the dashboard and writes results to a JSON cache file. The dashboard can consume that file instead of performing checks on demand.

## Usage

Run a single check and write output:

```bash
python health_checker.py --output status_cache.json
```

Run continuously every 2 minutes:

```bash
python health_checker.py --interval 120 --output status_cache.json
```

Run continuously while exposing a status API for the dashboard:

```bash
python health_checker.py --interval 120 --output status_cache.json --serve --api-port 8001
```

## Config

Copy `services.example.json` to `services.json` and replace placeholder values with your real lab targets. Local `services.json` is excluded by `.gitignore` so actual addresses do not go public.

The checker reads service definitions from `services.json` by default and lab host metadata from `~/Information/hosts.txt`.

## Dashboard integration

Point the dashboard at the generated `status_cache.json` file, or copy it into the dashboard workspace for the dashboard to serve.

> Credentials and host metadata should remain local. Do not commit `~/Information/credentials.txt`, `~/Information/credentials_secret.txt`, or your local `services.json` to public repositories.

## Service types supported

- `ping`
- `tcp`
- `process`
- `service`
- `ssh`
- `ssh_cmd`
- `docker`
- `dmesg`
- `temperature`
