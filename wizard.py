#!/usr/bin/env python3
"""
kaneru-wizard: Service setup wizard for the Kaneru system.

Reads a new service's config files, asks integration questions, and outputs
a numbered list of manual steps required to integrate the service.

Usage:
    python3 wizard.py <path/to/service/directory>

The wizard uses Claude (via the `claude` CLI) to analyse config files and
infer details that are not statically determinable. Falls back gracefully
if Claude is unavailable.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

DIVIDER = "=" * 62
THIN = "-" * 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ask_yn(question: str) -> bool:
    while True:
        ans = input(f"  {question} [y/n]: ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  Please enter y or n.")


def read_file(path: Path) -> Optional[str]:
    try:
        return path.read_text()
    except (FileNotFoundError, PermissionError):
        return None


def call_claude(prompt: str) -> str:
    """Run the `claude` CLI with a non-interactive prompt. Returns '' on failure."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=90,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def analyse_with_claude(files: Dict[str, Optional[str]]) -> dict:
    """
    Ask Claude to extract structured metadata from the service files.
    Returns an empty dict if Claude is unavailable or the response is unparseable.
    """
    context = "\n\n".join(
        f"=== {name} ===\n{content}"
        for name, content in files.items()
        if content
    )
    if not context:
        return {}

    prompt = (
        "Analyse the following Kaneru service configuration files and return "
        "ONLY a JSON object (no markdown, no explanation) with these fields:\n\n"
        "{\n"
        '  "service_name": "e.g. shipping_gateway",\n'
        '  "port": 8334,\n'
        '  "uses_db": true,\n'
        '  "uses_redis": true,\n'
        '  "jwt_callers": ["kaneru_gateway"],\n'
        '  "extra_env_vars": ["GOOGLE_API_KEY"],\n'
        '  "notes": "brief integration notes or null"\n'
        "}\n\n"
        "Rules:\n"
        "- service_name: from jwt_config.json service_name field, or infer from README/directory name\n"
        "- port: from config.json port field, or README/.env.example\n"
        "- uses_db: true if mysql/postgresql imports or DB references are present\n"
        "- uses_redis: true if redis imports or REDIS references are present\n"
        "- jwt_callers: keys from jwt_config.json permissions map\n"
        "- extra_env_vars: env vars beyond DB_HOST, REDIS_HOST, and *_PEM_PATH vars\n"
        "- Use null for any field you cannot determine\n\n"
        f"{context}"
    )

    response = call_claude(prompt)
    try:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return json.loads(match.group())
    except (json.JSONDecodeError, AttributeError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Docker-compose block generation
# ---------------------------------------------------------------------------

def pem_env_var(caller: str) -> str:
    return f"{caller.upper()}_PEM_PATH"


def pem_container_path(caller: str) -> str:
    """
    Derive the in-container path for a caller's public PEM.
    e.g. kaneru_gateway -> /run/secrets/kaneru/public.pem
         order_gateway  -> /run/secrets/order/public.pem
         api_gateway    -> /run/secrets/api/public.pem
    """
    prefix = caller.replace("_gateway", "").replace("_", "/")
    return f"/run/secrets/{prefix}/public.pem"


def build_compose_block(
    service_name: str,
    port: int,
    uses_db: bool,
    uses_redis: bool,
    jwt_callers: List[str],
    extra_env_vars: List[str],
    is_gateway_facing: bool,
) -> str:
    image = service_name.replace("_", "-")

    lines = [
        f"  {image}:",
        f"    image: {image}",
        f"    container_name: {image}",
        f"    ports:",
        f'      - "{port}:{port}"',
    ]

    if uses_db or uses_redis:
        lines += [
            "    extra_hosts:",
            '      - "host.docker.internal:host-gateway"',
        ]

    env_lines: List[str] = []
    if uses_db:
        env_lines.append("      DB_HOST: host.docker.internal")
    if uses_redis:
        env_lines.append("      REDIS_HOST: host.docker.internal")
    for caller in jwt_callers:
        env_lines.append(
            f"      {pem_env_var(caller)}: {pem_container_path(caller)}"
        )
    for var in extra_env_vars:
        env_lines.append(f"      {var}: <SET_VALUE>")

    if env_lines:
        lines.append("    environment:")
        lines.extend(env_lines)

    if jwt_callers:
        lines += [
            "    volumes:",
            "      - ../secrets/jwt/:/run/secrets:ro",
        ]

    if is_gateway_facing:
        lines += ["    depends_on:", "      - kaneru-gateway"]

    lines += ["    networks: [kaneru_net]", "    restart: unless-stopped"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step printing
# ---------------------------------------------------------------------------

def print_step(num: int, title: str, body: List[str]) -> None:
    print(f"Step {num}: {title}")
    print(THIN)
    for line in body:
        print(line)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: wizard.py <path/to/service/directory>")
        sys.exit(1)

    service_dir = Path(sys.argv[1]).resolve()
    if not service_dir.is_dir():
        print(f"Error: not a directory: {service_dir}")
        sys.exit(1)

    print(f"\n{DIVIDER}")
    print("  Kaneru Service Setup Wizard")
    print(f"  Service directory: {service_dir}")
    print(DIVIDER)

    # Read config files
    file_names = [
        "README.md",
        "jwt_config.json",
        "config.json",
        ".env.example",
        "requirements.txt",
    ]
    files: Dict[str, Optional[str]] = {
        name: read_file(service_dir / name) for name in file_names
    }

    found = [name for name, content in files.items() if content]
    print(f"\n  Config files found: {', '.join(found) if found else 'none'}")

    # Parse JSON configs statically
    jwt_cfg: Optional[dict] = None
    if files["jwt_config.json"]:
        try:
            jwt_cfg = json.loads(files["jwt_config.json"])
        except json.JSONDecodeError:
            print("  Warning: jwt_config.json is not valid JSON.")

    cfg: Optional[dict] = None
    if files["config.json"]:
        try:
            cfg = json.loads(files["config.json"])
        except json.JSONDecodeError:
            print("  Warning: config.json is not valid JSON.")

    # Enrich with Claude
    print("\n  Analysing with Claude...")
    ai = analyse_with_claude(files)
    if not ai:
        print("  (Claude unavailable or returned no usable data — proceeding with static analysis)")

    # Resolve service metadata, preferring static parse over AI
    service_name: str = (
        (jwt_cfg or {}).get("service_name")
        or ai.get("service_name")
        or service_dir.name
    )
    port: Optional[int] = (cfg or {}).get("port") or ai.get("port")
    uses_db: bool = bool(ai.get("uses_db", False))
    uses_redis: bool = bool(ai.get("uses_redis", False))
    jwt_callers: List[str] = (
        list((jwt_cfg or {}).get("permissions", {}).keys())
        or ai.get("jwt_callers", [])
    )
    extra_env_vars: List[str] = ai.get("extra_env_vars") or []

    print(f"\n  Service name : {service_name}")
    print(f"  Port         : {port or '(not determined)'}")
    print(f"  Uses DB      : {uses_db}")
    print(f"  Uses Redis   : {uses_redis}")
    print(f"  JWT callers  : {', '.join(jwt_callers) or '(none detected)'}")
    if extra_env_vars:
        print(f"  Extra env    : {', '.join(extra_env_vars)}")
    if ai.get("notes"):
        print(f"\n  Notes: {ai['notes']}")

    # -----------------------------------------------------------------------
    # Integration questions
    # -----------------------------------------------------------------------
    print(f"\n{THIN}")
    print("Integration questions:\n")

    is_client_facing = ask_yn("Is this service client-facing?")
    is_web = False
    is_gateway_facing = False
    if is_client_facing:
        is_web = ask_yn("Is the client a web client (Next.js)?")
        is_gateway_facing = ask_yn("Is the client kaneru_gateway-facing (mobile/Flutter)?")

    # -----------------------------------------------------------------------
    # Generate steps
    # -----------------------------------------------------------------------
    print(f"\n{DIVIDER}")
    print(f"  Setup Steps — {service_name}")
    print(DIVIDER)
    print()

    step = 0

    # ------------------------------------------------------------------
    # Step: Generate JWT key pair
    # ------------------------------------------------------------------
    step += 1
    key_base = f"../secrets/jwt/{service_name}"
    print_step(step, f"Generate JWT key pair for {service_name}", [
        f"  $ mkdir -p {key_base}",
        f"  $ openssl genrsa -out {key_base}/private.pem 2048",
        f"  $ openssl rsa -in {key_base}/private.pem -pubout \\",
        f"            -out {key_base}/public.pem",
        "",
        "  private.pem — kept secret, used by this service to sign outbound JWTs.",
        "  public.pem  — distributed to any caller that needs to verify this service's JWTs.",
        "  NEVER commit PEM files to source control.",
    ])

    # ------------------------------------------------------------------
    # Step: Verify caller public keys exist
    # ------------------------------------------------------------------
    if jwt_callers:
        step += 1
        lines = [
            "  jwt_config.json declares the following permitted callers.",
            "  Each caller's public key must be present on disk before deployment:",
            "",
        ]
        for caller in jwt_callers:
            caller_slug = caller.replace("_gateway", "")
            lines += [
                f"    Caller : {caller}",
                f"    Env var: {pem_env_var(caller)}",
                f"    Source : ../secrets/jwt/{caller_slug}/public.pem",
                "",
            ]
        lines.append(
            "  If a caller's key directory does not exist, run the equivalent of Step 1"
        )
        lines.append("  in that service's setup to generate and place the key.")
        print_step(step, "Verify caller public keys exist", lines)

    # ------------------------------------------------------------------
    # Step: kaneru_gateway config.json
    # ------------------------------------------------------------------
    if is_gateway_facing:
        step += 1
        route = service_name.replace("_gateway", "")
        entry = json.dumps(
            {"route": route, "url": "127.0.0.1", "port": port or 0},
            indent=4,
        )
        entry_indented = "\n".join("    " + l for l in entry.splitlines())
        body = [
            "  In kaneru_gateway/config.json, add to the 'services' array:",
            "",
            entry_indented,
            "",
            f"  This routes requests for /{route}/* to this service on port {port or '<port>'}.",
        ]
        if port == 0 or port is None:
            body.append("  Replace the port value once the service port is confirmed.")
        if "kaneru_gateway" not in jwt_callers:
            body += [
                "",
                "  ALSO: 'kaneru_gateway' is not listed in this service's jwt_config.json.",
                "  See the next step to add it.",
            ]
        print_step(step, "Add service to kaneru_gateway/config.json", body)

        # ------------------------------------------------------------------
        # Step: Add kaneru_gateway to jwt_config.json permissions
        # ------------------------------------------------------------------
        if "kaneru_gateway" not in jwt_callers:
            step += 1
            route_name = service_name.replace("_gateway", "")
            example = {
                "permissions": {
                    "kaneru_gateway": [
                        f"{route_name}.health",
                        f"<add other {route_name}.* scopes as needed>",
                    ]
                }
            }
            perm_str = json.dumps(example, indent=4)
            perm_indented = "\n".join("    " + l for l in perm_str.splitlines())
            print_step(
                step,
                f"Add kaneru_gateway to {service_name}/jwt_config.json permissions",
                [
                    "  Merge the following into the 'permissions' map in jwt_config.json:",
                    "",
                    perm_indented,
                    "",
                    "  Scope convention: <service_name>.<route_suffix>",
                    "  Only list the scopes kaneru_gateway actually needs to call.",
                    "  Rebuilding the image is required after editing jwt_config.json.",
                ],
            )

    # ------------------------------------------------------------------
    # Step: Next.js (TODO placeholder)
    # ------------------------------------------------------------------
    if is_web:
        step += 1
        print_step(step, "[TODO] Configure Next.js gateway", [
            "  Next.js configuration is not yet automated by this wizard.",
            "  Manual steps required:",
            "    • Add an API route handler for this service in the Next.js application.",
            "    • Configure the service URL and port in Next.js environment config.",
            "    • Implement JWT signing for outbound requests to this service.",
        ])

    # ------------------------------------------------------------------
    # Step: docker-compose block
    # ------------------------------------------------------------------
    step += 1
    if port:
        block = build_compose_block(
            service_name,
            port,
            uses_db,
            uses_redis,
            jwt_callers,
            extra_env_vars,
            is_gateway_facing,
        )
        print_step(step, "Add service block to docker-compose.yaml", [
            "  Add the following to the 'services:' section of docker-compose.yaml:",
            "",
            block,
            "",
            "  Review the volume path (../secrets/jwt/) — adjust if your secrets",
            "  directory is located elsewhere relative to the compose file.",
            "  Add any service-specific extra_env_vars values.",
        ])
    else:
        print_step(step, "Add service block to docker-compose.yaml", [
            "  Could not determine the service port.",
            "  Locate it in config.json or the README, then model the block on an",
            "  existing entry in docker-compose.yaml, e.g. kaneru-jobs or shipping-gateway.",
            "  Key fields to include:",
            f"    image: {service_name.replace('_', '-')}",
            "    ports, extra_hosts, environment (*_PEM_PATH vars), volumes, networks",
        ])

    # ------------------------------------------------------------------
    # Step: Environment variable summary
    # ------------------------------------------------------------------
    step += 1
    env_summary: List[str] = []
    if uses_db:
        env_summary.append("  DB_HOST=host.docker.internal")
    if uses_redis:
        env_summary.append("  REDIS_HOST=host.docker.internal")
    for caller in jwt_callers:
        caller_slug = caller.replace("_gateway", "")
        env_summary.append(
            f"  {pem_env_var(caller)}=../secrets/jwt/{caller_slug}/public.pem"
        )
    for var in extra_env_vars:
        env_summary.append(f"  {var}=<set appropriate value>")
    if not env_summary:
        env_summary.append("  (no environment variables identified beyond docker-compose defaults)")

    print_step(step, "Environment variable checklist", [
        "  Ensure these are set in docker-compose.yaml or your deployment config:",
        "",
        *env_summary,
    ])

    # ------------------------------------------------------------------
    # Step: Build Docker image
    # ------------------------------------------------------------------
    step += 1
    image = service_name.replace("_", "-")
    print_step(step, f"Build Docker image '{image}'", [
        f"  $ cd {service_dir}",
        f"  $ docker build -t {image} .",
        "",
        "  Run from the service directory containing the Dockerfile.",
        "  Re-run after any change to jwt_config.json or requirements.txt.",
    ])

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(DIVIDER)
    print(f"  {step} steps generated for: {service_name}")
    print(DIVIDER)
    print()


if __name__ == "__main__":
    main()
