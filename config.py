"""OpenFGA database setup on the DocAI RDS host (separate `openfga` database, never the app's `postgres` DB).

Usage:  python config.py
  1. connects to the RDS host (directly, or via jump server when JUMP_HOST is set)
  2. creates the OpenFGA database if it does not exist
  3. reports whether OpenFGA migrations ran in it and whether the OpenFGA API is healthy

Env (.env): DB_USER, DB_PASSWORD (required), DB_HOST, DB_PORT, DB_SSLMODE, OPENFGA_DB_NAME, FGA_URL,
            JUMP_HOST, JUMP_PORT, JUMP_USER, JUMP_PASSWORD | JUMP_KEY_FILE [+ JUMP_KEY_PASSWORD]
"""
import os
import sys
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_DB_HOST = "hex-aws-docaidb-dev-1f37ed-use1-rds-01.cvkiu8yy2qpg.us-east-1.rds.amazonaws.com"
DB_HOST = os.getenv("DB_HOST", _DEFAULT_DB_HOST)
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_SSLMODE = os.getenv("DB_SSLMODE", "require")
OPENFGA_DB_NAME = os.getenv("OPENFGA_DB_NAME", "openfga")
FGA_URL = os.getenv("FGA_URL", "http://localhost:8080")

_jump_tunnel = None


def _start_jump_tunnel():
    """POC / local: when JUMP_HOST is set, open an SSH tunnel through the jump server to the database (once per process).
    Returns (host, port) to connect to. Servers without JUMP_HOST connect directly."""
    global _jump_tunnel
    if _jump_tunnel is None:
        import atexit
        from sshtunnel import SSHTunnelForwarder

        options = {
            "ssh_address_or_host": (os.getenv("JUMP_HOST"), int(os.getenv("JUMP_PORT", "22"))),
            "ssh_username": os.getenv("JUMP_USER"),
            "remote_bind_address": (os.getenv("DB_REMOTE_HOST", _DEFAULT_DB_HOST), int(os.getenv("DB_REMOTE_PORT", "5432"))),
            "local_bind_address": ("127.0.0.1", 0),  # free local port chosen automatically
            "set_keepalive": 30,
        }
        if os.getenv("JUMP_KEY_FILE"):
            options["ssh_pkey"] = os.getenv("JUMP_KEY_FILE")
            if os.getenv("JUMP_KEY_PASSWORD"):
                options["ssh_private_key_password"] = os.getenv("JUMP_KEY_PASSWORD")
        else:
            options["ssh_password"] = os.getenv("JUMP_PASSWORD")

        _jump_tunnel = SSHTunnelForwarder(**options)
        _jump_tunnel.start()
        atexit.register(_jump_tunnel.stop)
        print(f"Database via jump server {os.getenv('JUMP_HOST')} -> local port {_jump_tunnel.local_bind_port}")
    return "127.0.0.1", _jump_tunnel.local_bind_port


def db_address():
    """(host, port) to reach Postgres from this process: jump tunnel if JUMP_HOST is set, else DB_HOST/DB_PORT."""
    if os.getenv("JUMP_HOST"):
        return _start_jump_tunnel()
    return DB_HOST, DB_PORT


def openfga_datastore_uri(host=DB_HOST, port=DB_PORT):
    """Datastore URI for OPENFGA_DATASTORE_URI (the openfga/migrate containers)."""
    return (
        f"postgres://{quote(DB_USER, safe='')}:{quote(DB_PASSWORD, safe='')}"
        f"@{host}:{port}/{OPENFGA_DB_NAME}?sslmode={DB_SSLMODE}"
    )


def _connect(dbname):
    import psycopg2

    host, port = db_address()
    return psycopg2.connect(
        host=host, port=port, user=DB_USER, password=DB_PASSWORD, dbname=dbname, sslmode=DB_SSLMODE, connect_timeout=10
    )


def ensure_database():
    """Create OPENFGA_DB_NAME on the host if it is missing. Returns True if it was created."""
    from psycopg2 import sql

    if OPENFGA_DB_NAME == "postgres":
        sys.exit("OPENFGA_DB_NAME must not be 'postgres' (that is the DocAI app database).")

    conn = _connect("postgres")
    conn.autocommit = True  # CREATE DATABASE cannot run inside a transaction
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (OPENFGA_DB_NAME,))
            if cur.fetchone():
                print(f"database '{OPENFGA_DB_NAME}' already exists")
                return False
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(OPENFGA_DB_NAME)))
            print(f"database '{OPENFGA_DB_NAME}' created")
            return True
    finally:
        conn.close()


def check_migrations():
    """Report whether `openfga migrate` has run in the OpenFGA database."""
    conn = _connect(OPENFGA_DB_NAME)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.goose_db_version'), to_regclass('public.tuple')")
            goose, tuple_tbl = cur.fetchone()
            if not (goose and tuple_tbl):
                print("migrations: NOT applied -> run `docker compose up -d` (the migrate service does it)")
                return False
            cur.execute("SELECT max(version_id) FROM goose_db_version WHERE is_applied")
            version = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM store")
            stores = cur.fetchone()[0]
            print(f"migrations: applied (schema version {version}), stores={stores}")
            return True
    finally:
        conn.close()


def check_openfga():
    """Report whether the OpenFGA API at FGA_URL is up."""
    import httpx

    try:
        r = httpx.get(f"{FGA_URL}/healthz", timeout=5)
        r.raise_for_status()
        print(f"openfga {FGA_URL}: {r.json().get('status', r.text)}")
        stores = httpx.get(f"{FGA_URL}/stores", timeout=5).json().get("stores", [])
        print(f"openfga stores: {len(stores)}")
        return True
    except httpx.HTTPError as e:
        print(f"openfga {FGA_URL}: NOT reachable ({e.__class__.__name__}) -> start it with `docker compose up -d`")
        return False


if __name__ == "__main__":
    if not (DB_USER and DB_PASSWORD):
        sys.exit("Set DB_USER and DB_PASSWORD in .env")
    print(f"db host: {DB_HOST}:{DB_PORT}  database: {OPENFGA_DB_NAME}")
    ensure_database()
    check_migrations()
    check_openfga()
    if not os.getenv("OPENFGA_DATASTORE_URI"):
        masked = openfga_datastore_uri().replace(quote(DB_PASSWORD, safe=""), "****")
        print(f"\nAdd to .env so docker compose uses this DB:\nOPENFGA_DATASTORE_URI={masked}  (put the real password)")