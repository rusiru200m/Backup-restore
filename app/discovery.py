"""Read-only discovery of Docker PostgreSQL restore targets over verified SSH."""
from __future__ import annotations

import json

from .models import DiscoverDatabasesRequest, NAME_RE
from .remote import RemoteError, docker_args
from .workflow import WorkflowError, connect_destination

# Read only the two non-secret startup hints, never docker inspect's full Env or
# POSTGRES_PASSWORD. Both values are treated as data and validated below.
STARTUP_HINTS = 'printf "%s\\n" "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}"'
DATABASE_QUERY = (
    "SELECT json_build_object('database_user', current_user, 'databases', "
    "COALESCE(json_agg(datname ORDER BY datname), '[]'::json)) "
    "FROM pg_database WHERE NOT datistemplate AND datallowconn "
    "AND has_database_privilege(current_user, oid, 'CONNECT')"
)
CONTAINER_FORMAT = '{"Names":{{json .Names}},"Image":{{json .Image}}}'


def discover_databases(request: DiscoverDatabasesRequest) -> dict:
    items = []
    warnings = []
    postgres_containers = 0
    with connect_destination(request.server) as remote:
        output = remote.run(["docker", "ps", "--format", CONTAINER_FORMAT], timeout=20, require_complete=True)
        try:
            containers = [json.loads(line) for line in output.splitlines() if line.strip()]
            if any(not isinstance(container, dict) or not isinstance(container.get("Names"), str)
                   or not isinstance(container.get("Image"), str) for container in containers):
                raise ValueError("Invalid container metadata")
        except (ValueError, TypeError) as exc:
            raise WorkflowError("Could not read the running Docker container list") from exc

        for container in containers:
            name, image = container["Names"], container["Image"]
            if not NAME_RE.fullmatch(name):
                warnings.append({"container": name, "message": "Container name is unsupported by the restore workflow"})
                continue
            try:
                version = remote.run(docker_args(name, "pg_restore", "--version"), timeout=10)
                if "pg_restore (PostgreSQL)" not in version:
                    continue
            except RemoteError:
                if "postgres" in image.lower():
                    warnings.append({"container": name, "message": "PostgreSQL restore tools are unavailable in this running container"})
                continue
            postgres_containers += 1
            configured_user, configured_db = "postgres", "postgres"
            try:
                hints = remote.run(docker_args(name, "sh", "-c", STARTUP_HINTS), timeout=10, require_complete=True).splitlines()
                if len(hints) == 2 and all(NAME_RE.fullmatch(value) for value in hints):
                    configured_user, configured_db = hints
            except RemoteError:
                pass

            candidates = []
            if request.database_user:
                candidates.extend([(request.database_user, request.database or configured_db),
                                   (request.database_user, "postgres"), (request.database_user, "template1")])
            candidates.extend([(configured_user, configured_db), (configured_user, "postgres"),
                               (configured_user, "template1"), ("postgres", "postgres"), ("postgres", "template1")])
            catalog = None
            for user, database in dict.fromkeys(candidates):
                try:
                    result = remote.run([
                        "docker", "exec", "-u", "0", "-e", "PGCONNECT_TIMEOUT=5", name,
                        "psql", "-X", "-w", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                        "-U", user, "-d", database, "-c", DATABASE_QUERY,
                    ], timeout=12, require_complete=True)
                    parsed = json.loads(result)
                    if not isinstance(parsed, dict) or not isinstance(parsed.get("database_user"), str) or not NAME_RE.fullmatch(parsed["database_user"]):
                        continue
                    if not isinstance(parsed.get("databases"), list) or any(not isinstance(db, str) for db in parsed["databases"]):
                        continue
                    catalog = parsed
                    break
                except (RemoteError, ValueError, TypeError):
                    continue
            if catalog is None:
                warnings.append({"container": name, "message": "Could not list databases using the detected roles. Configure local PostgreSQL authentication or enter a known role/database in the advanced fields and retry."})
                continue
            unsupported = 0
            for database in sorted(set(catalog["databases"])):
                if not NAME_RE.fullmatch(database):
                    unsupported += 1
                    continue
                items.append({"container": name, "database": database,
                              "database_user": catalog["database_user"], "image": image})
            if unsupported:
                warnings.append({"container": name, "message": f"{unsupported} database name(s) are unsupported by the restore workflow"})
    return {"items": sorted(items, key=lambda item: (item["container"].casefold(), item["database"].casefold())),
            "scanned_containers": len(containers), "postgres_containers": postgres_containers, "warnings": warnings,
            "note": "Discovery reads database names and CONNECT privileges. Test the selected destination before restoring; dump and replacement privileges must also be configured."}
