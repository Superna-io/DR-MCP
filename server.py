"""
Superna Eyeglass Failover MCP Server

Exposes the Superna Eyeglass REST API (/sera/) as MCP tools, focusing on
failover, DR testing, readiness checks, and related node/job operations.

Authentication: API token via 'api_key' header (format: igls-...)

Config: reads from superna_mcp.json in the same directory as this file,
        falls back to environment variables for all settings.
"""

import os
import json
import queue
import logging
import logging.handlers
import threading
import traceback
import functools
import urllib3
import requests
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Version ──────────────────────────────────────────────────────────────────

BUILD = "1.1.8"


# ─── Synchronous trace ────────────────────────────────────────────────────────
# Writes DIRECTLY to superna_trace.log — no queue, no pipe, no buffering.
# This is the only reliable way to see exactly which line is blocking when
# both the stdout pipe AND the log queue are potentially stalled.
# Uses a threading.Lock so concurrent tool threads don't interleave writes.

import time as _time

_trace_lock = threading.Lock()


def _trace(msg: str) -> None:
    """Synchronous direct-to-file trace — survives stdout/log queue stalls."""
    try:
        ts = _time.strftime("%H:%M:%S")
        tname = threading.current_thread().name
        line = f"{ts}  [{tname}]  {msg}\n"
        trace_path = Path(os.path.abspath(__file__)).parent / "superna_trace.log"
        with _trace_lock:
            with open(trace_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
    except Exception:
        pass


# ─── Non-blocking stdout ──────────────────────────────────────────────────────
# print(..., flush=True) inside a tool worker thread will BLOCK when the stdout
# pipe (to the GUI) is full.  This mirrors the QueueHandler fix for logging:
# _nb_print() enqueues the message and returns immediately; a daemon thread does
# the actual write.  Tool threads can NEVER block on console output.

_print_queue: queue.Queue = queue.Queue(-1)   # unbounded


def _nb_print(msg: str) -> None:
    """Non-blocking console print — never stalls the calling thread."""
    _print_queue.put_nowait(msg)


def _stdout_worker() -> None:
    while True:
        try:
            msg = _print_queue.get(block=True, timeout=1)
            print(msg, flush=True)
            _print_queue.task_done()
        except queue.Empty:
            pass
        except Exception:
            pass


threading.Thread(target=_stdout_worker, daemon=True, name="stdout-writer").start()

# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    # Use abspath so Path(__file__) works even when invoked with a relative path
    log_path = Path(os.path.abspath(__file__)).parent / "superna_mcp.log"
    _nb_print(f"[superna_mcp] log -> {log_path}")

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # Use QueueHandler so that log.info() / log.debug() in tool worker
    # threads NEVER block on file I/O.  The GUI process also writes to the
    # same log file; on Windows, concurrent cross-process file flushes can
    # cause the FileHandler.flush() call to block indefinitely, freezing the
    # tool thread mid-execution.  QueueHandler enqueues the record and
    # returns immediately; a daemon QueueListener thread drains the queue
    # and does the actual file write in the background.
    _log_queue: queue.Queue = queue.Queue(-1)   # unbounded
    queue_handler = logging.handlers.QueueHandler(_log_queue)
    listener = logging.handlers.QueueListener(_log_queue, fh, respect_handler_level=True)
    listener.start()

    # Our own logger
    logger = logging.getLogger("superna_mcp")
    logger.setLevel(logging.DEBUG)
    # CRITICAL: disable propagation to the root logger.
    # uvicorn/starlette configure the root logger with a direct StreamHandler
    # (writing to stdout/stderr) during server startup.  With propagate=True
    # (the default), every log.info() / log.debug() call in a tool worker
    # thread ALSO writes synchronously to that StreamHandler — which blocks
    # when the stdout pipe to the GUI is full.  This is what caused tool
    # threads to hang on the 3rd call: the QueueHandler was non-blocking but
    # the propagated root-logger write was not.
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(queue_handler)

    # Capture FastMCP / uvicorn error logs into the same file at WARNING+.
    # Do NOT set asyncio or uvicorn.access to DEBUG — synchronous writes
    # inside the event loop block it from flushing SSE TCP sends.
    for lib_name in ("mcp", "uvicorn", "uvicorn.error", "fastapi", "starlette"):
        lib_log = logging.getLogger(lib_name)
        lib_log.setLevel(logging.WARNING)
        lib_log.propagate = False
        if not any(isinstance(h, logging.handlers.QueueHandler) for h in lib_log.handlers):
            lib_log.addHandler(queue_handler)

    return logger

log = _setup_logging()


_registered_tools: list[str] = []


def _mcp_tool(func):
    """
    Drop-in replacement for @mcp.tool().

    Runs the (blocking) tool function in an anyio worker thread so HTTP
    requests, JSON parsing, str() conversion, and file logging never block
    the event loop.  The event loop only awaits the thread future, keeping
    SSE message delivery fully responsive.

    ALL work — call, str(), log writes — is done inside _run() in the
    thread.  cancellable=True lets anyio release the capacity limiter
    immediately if the surrounding scope is cancelled (e.g. client
    disconnect), preventing the limiter from being held while cleanup runs.
    """
    import anyio

    @functools.wraps(func)
    async def wrapper(**kwargs):
        def _run():
            # These prints go to the server console / piped stdout so the
            # GUI can stream them live.  They run inside the worker thread,
            # not the event loop.
            _trace(f"_run ENTER  {func.__name__}")
            _nb_print(f"[TOOL >>] {func.__name__}  {str(kwargs)[:200]}")
            log.info("TOOL CALL  %-38s  args=%s", func.__name__, kwargs)
            try:
                _trace(f"_run calling func  {func.__name__}")
                result = func(**kwargs)
                _trace(f"_run func returned  {func.__name__}")
                _nb_print(f"[TOOL RES] {func.__name__} returned")
                # Use _safe_snippet — never calls str() on the full object,
                # which can be very slow for large SyncIQ readiness payloads.
                _trace(f"_run _safe_snippet  {func.__name__}")
                snippet = _safe_snippet(result, 300)
                _trace(f"_run _safe_snippet done  {func.__name__}")
                _nb_print(f"[TOOL <<] {func.__name__}  OK  {snippet}")
                _trace(f"_run log.info TOOL OK  {func.__name__}")
                log.info("TOOL OK    %-38s  result=%s", func.__name__, snippet)
                _trace(f"_run RETURN  {func.__name__}")
                return result
            except BaseException as exc:
                _trace(f"_run EXCEPT  {func.__name__}  {type(exc).__name__}: {exc}")
                _nb_print(f"[TOOL !!] {func.__name__}  {type(exc).__name__}: {exc}")
                if isinstance(exc, BaseExceptionGroup):
                    for i, sub in enumerate(exc.exceptions, 1):
                        log.error("TOOL ERROR %-38s  sub[%d] %s: %s\n%s",
                                  func.__name__, i, type(sub).__name__, sub,
                                  "".join(traceback.format_exception(type(sub), sub, sub.__traceback__)))
                else:
                    log.error("TOOL ERROR %-38s  %s: %s\n%s",
                              func.__name__, type(exc).__name__, exc, traceback.format_exc())
                raise

        # This print runs on the event loop thread — useful to confirm the
        # event loop is alive when a second tool call arrives.
        _nb_print(f"[TOOL ..] {func.__name__} — dispatching to thread")
        _trace(f"wrapper AWAIT run_sync  {func.__name__}")
        result = await anyio.to_thread.run_sync(_run, cancellable=True)
        _trace(f"wrapper run_sync RETURNED  {func.__name__}")
        _nb_print(f"[TOOL OK] {func.__name__} — thread returned")
        return result

    registered = mcp.tool()(wrapper)
    _registered_tools.append(func.__name__)
    log.debug("Registered tool [%d]: %s", len(_registered_tools), func.__name__)
    return registered


# ─── Configuration ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load superna_mcp.json from the same directory as this script."""
    config_path = Path(__file__).parent / "superna_mcp.json"
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as exc:
            log.warning("Failed to load config %s: %s", config_path, exc)
    return {}

_cfg = _load_config()

EYEGLASS_HOST      = _cfg.get("eyeglass_host")      or os.environ.get("EYEGLASS_HOST", "igls")
EYEGLASS_API_TOKEN = _cfg.get("eyeglass_api_token") or os.environ.get("EYEGLASS_API_TOKEN", "")
EYEGLASS_VERIFY_SSL = (
    _cfg.get("eyeglass_verify_ssl")
    if "eyeglass_verify_ssl" in _cfg
    else os.environ.get("EYEGLASS_VERIFY_SSL", "false").lower() == "true"
)
MCP_PORT = int(_cfg.get("mcp_port") or os.environ.get("MCP_PORT", 8000))

BASE_URL = f"https://{EYEGLASS_HOST}/sera"

# ─── API Client ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"api_key": EYEGLASS_API_TOKEN, "Content-Type": "application/json"}


def _safe_snippet(obj, max_chars: int = 300) -> str:
    """Return a short, safe string representation — never calls str() on the full object."""
    try:
        if isinstance(obj, list):
            return f"[list len={len(obj)}] first={str(obj[0])[:100] if obj else '(empty)'}"
        if isinstance(obj, dict):
            keys = list(obj.keys())[:6]
            return f"{{dict keys={keys}}}"
        return str(obj)[:max_chars]
    except Exception:
        return "<repr failed>"


def _log_response(method: str, url: str, params, resp) -> None:
    _trace(f"_log_response ENTER  {method} {url}  status={resp.status_code}")
    body_bytes = len(resp.content)
    _trace(f"_log_response len(resp.content)={body_bytes}")
    log.info("%-6s %s  params=%s  -> HTTP %s  body=%d bytes",
             method, url, params, resp.status_code, body_bytes)
    _trace(f"_log_response log.info done")
    _nb_print(f"[HTTP BDY ] body={body_bytes} bytes")
    try:
        # Only log the body text for small responses — large responses
        # keep the debug line brief to avoid slow str operations on the log thread.
        if body_bytes <= 8000:
            log.debug("       response body: %s", resp.text[:2000])
        else:
            log.debug("       response body: <large %d bytes — truncated>", body_bytes)
    except Exception:
        pass
    _trace(f"_log_response EXIT")


def _log_error(method: str, url: str, params, exc: Exception) -> None:
    log.error("%-6s %s  params=%s  → %s: %s", method, url, params, type(exc).__name__, exc)
    log.debug(traceback.format_exc())


_TIMEOUT = 15  # seconds — short enough to surface errors quickly


def _get(path: str, params: dict = None) -> dict | list:
    url = f"{BASE_URL}{path}"
    _trace(f"_get ENTER  {url}")
    _nb_print(f"[HTTP GET ] {url}")
    try:
        resp = requests.get(url, headers=_headers(), params=params,
                            verify=EYEGLASS_VERIFY_SSL, timeout=_TIMEOUT)
        _trace(f"_get GOT  {url}  status={resp.status_code}")
        _nb_print(f"[HTTP GOT ] {url}  {resp.status_code}")
        _log_response("GET", url, params, resp)
        _nb_print(f"[HTTP LOG ] logged")
        _trace(f"_get raise_for_status  status={resp.status_code}")
        resp.raise_for_status()
        _trace(f"_get before resp.json()")
        _nb_print(f"[HTTP STS ] status ok")
        data = resp.json()
        _trace(f"_get after resp.json()  type={type(data).__name__}")
        _nb_print(f"[HTTP JSN ] json parsed  type={type(data).__name__}")
        _trace(f"_get RETURN  {url}")
        return data
    except Exception as exc:
        _trace(f"_get EXCEPT  {url}  {type(exc).__name__}: {exc}")
        _nb_print(f"[HTTP ERR ] GET {url}  {type(exc).__name__}: {exc}")
        _log_error("GET", url, params, exc)
        raise


def _post(path: str, params: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    _nb_print(f"[HTTP POST] {url}")
    try:
        resp = requests.post(url, headers=_headers(), params=params,
                             verify=EYEGLASS_VERIFY_SSL, timeout=_TIMEOUT)
        _nb_print(f"[HTTP GOT ] {url}  {resp.status_code}")
        _log_response("POST", url, params, resp)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _nb_print(f"[HTTP ERR ] POST {url}  {type(exc).__name__}: {exc}")
        _log_error("POST", url, params, exc)
        raise


def _delete(path: str, params: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    _nb_print(f"[HTTP DEL ] {url}")
    try:
        resp = requests.delete(url, headers=_headers(), params=params, json={},
                               verify=EYEGLASS_VERIFY_SSL, timeout=_TIMEOUT)
        _nb_print(f"[HTTP GOT ] {url}  {resp.status_code}")
        _log_response("DELETE", url, params, resp)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _nb_print(f"[HTTP ERR ] DELETE {url}  {type(exc).__name__}: {exc}")
        _log_error("DELETE", url, params, exc)
        raise


def _put(path: str, params: dict = None) -> dict:
    url = f"{BASE_URL}{path}"
    _nb_print(f"[HTTP PUT ] {url}")
    try:
        resp = requests.put(url, headers=_headers(), params=params,
                            verify=EYEGLASS_VERIFY_SSL, timeout=_TIMEOUT)
        _nb_print(f"[HTTP GOT ] {url}  {resp.status_code}")
        _log_response("PUT", url, params, resp)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        _nb_print(f"[HTTP ERR ] PUT {url}  {type(exc).__name__}: {exc}")
        _log_error("PUT", url, params, exc)
        raise


def _clean(d: dict) -> dict:
    """Remove None values from a dict (so they aren't sent as query params)."""
    return {k: v for k, v in d.items() if v is not None}


# ─── MCP Server ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Superna Eyeglass Failover",
    instructions=(
        "Tools for managing Superna Eyeglass DR failover operations. "
        "You can list nodes/clusters, inspect policies and access zones, "
        "launch or cancel failover/rehearsal/DR-test jobs, check readiness, "
        "and retrieve job logs."
    ),
)

# ══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def health_check() -> dict:
    """Check the health status of the Superna Eyeglass appliance."""
    return _get("/v1/healthcheck")


# ══════════════════════════════════════════════════════════════════════════════
# ALARMS
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_active_alarms() -> list:
    """Return all currently active alarms on the Eyeglass appliance."""
    return _get("/v1/alarms/active")


@_mcp_tool
def list_historical_alarms() -> list:
    """Return historical (resolved) alarms from the Eyeglass appliance."""
    return _get("/v1/alarms/historical")


# ══════════════════════════════════════════════════════════════════════════════
# NODES (v1)  – Managed PowerScale / Isilon clusters
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_nodes() -> list:
    """
    List all Superna Eyeglass managed PowerScale/Isilon cluster nodes.

    Returns a list of Node objects with fields:
      - id:   unique cluster identifier (used as sourceid / targetid in failover jobs)
      - ip:   primary IP address
      - name: human-readable cluster name
    """
    return _get("/v1/nodes")


@_mcp_tool
def get_node(node_id: str) -> dict:
    """
    Retrieve details for a specific managed node.

    Args:
        node_id: The node ID (from list_nodes).
    """
    return _get(f"/v1/nodes/{node_id}")


@_mcp_tool
def list_node_policies(
    node_id: str,
    fo_readiness: bool = False,
) -> list:
    """
    List SyncIQ policies for a specific node, optionally including failover readiness detail.

    Args:
        node_id:      Node ID (from list_nodes).
        fo_readiness: When True, include detailed DR readiness status for each policy.
    """
    return _get(f"/v1/nodes/{node_id}/policies", params={"foReadiness": str(fo_readiness).lower()})


@_mcp_tool
def get_node_policy(node_id: str, policy_name: str) -> dict:
    """
    Retrieve a single SyncIQ policy by name on a given node.

    Args:
        node_id:     Node ID (from list_nodes).
        policy_name: Exact policy name.
    """
    return _get(f"/v1/nodes/{node_id}/policies/{policy_name}")


@_mcp_tool
def list_node_zones(
    node_id: str,
    fo_readiness: bool = False,
) -> list:
    """
    List Access Zones for a specific node, optionally including failover readiness detail.

    Args:
        node_id:      Node ID (from list_nodes).
        fo_readiness: When True, include detailed DR readiness status for each zone.
    """
    return _get(f"/v1/nodes/{node_id}/zones", params={"foReadiness": str(fo_readiness).lower()})


@_mcp_tool
def get_node_zone(node_id: str, zone_name: str) -> dict:
    """
    Retrieve a specific Access Zone by name on a given node.

    Args:
        node_id:   Node ID (from list_nodes).
        zone_name: Access zone name.
    """
    return _get(f"/v1/nodes/{node_id}/zones/{zone_name}")


@_mcp_tool
def list_node_pools(
    node_id: str,
    fo_readiness: bool = False,
) -> list:
    """
    List IP pools for a specific node, optionally including failover readiness detail.

    Args:
        node_id:      Node ID (from list_nodes).
        fo_readiness: When True, include detailed DR readiness status for each pool.
    """
    return _get(f"/v1/nodes/{node_id}/pools", params={"foReadiness": str(fo_readiness).lower()})


@_mcp_tool
def get_node_pool(node_id: str, pool_name: str) -> dict:
    """
    Retrieve a specific pool by name on a given node.

    Args:
        node_id:   Node ID (from list_nodes).
        pool_name: Pool name in 'groupName:subnetName:poolName' format.
    """
    return _get(f"/v1/nodes/{node_id}/pools/{pool_name}")


# ══════════════════════════════════════════════════════════════════════════════
# FAILOVER JOBS (v1)  – Legacy endpoint, policy-level failover
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_failover_jobs_v1(
    state: Optional[str] = None,
    success: Optional[bool] = None,
) -> list:
    """
    List failover jobs (v1 API).

    Args:
        state:   Filter by job state: 'all' | 'running' | 'finished'. Default: all.
        success: Filter by result: True = successful, False = failed.
    """
    return _get("/v1/jobs", params=_clean({"state": state, "success": success}))


@_mcp_tool
def create_failover_job_v1(
    sourceid: str,
    targetid: str,
    failovertarget: str,
    pool: Optional[str] = None,
    controlled: Optional[bool] = None,
    datasync: Optional[bool] = None,
    configsync: Optional[bool] = None,
    resyncprep: Optional[bool] = None,
    disablemirror: Optional[bool] = None,
    quotasync: Optional[bool] = None,
    blockonwarnings: bool = True,
    rollbackrenameshares: Optional[bool] = None,
    smbdataintegrity: Optional[bool] = None,
) -> dict:
    """
    Launch a new failover job (v1 API).

    Args:
        sourceid:            ID of the source (primary) node.
        targetid:            ID of the target (DR) node.
        failovertarget:      Access zone ID to fail over, OR comma-separated policy IDs.
        pool:                Pool name for pool-level failover ('group:subnet:pool').
        controlled:          True = controlled failover (operates on both source and target).
        datasync:            Run a final incremental data sync before failing over.
        configsync:          Run a configuration sync before failing over.
        resyncprep:          Create mirror policies on source for failback.
        disablemirror:       Disable mirror policies on the failover target.
        quotasync:           Fail over quotas to target.
        blockonwarnings:     Block the failover if warnings are detected (default True).
        rollbackrenameshares: Roll back renamed shares on failure.
        smbdataintegrity:    Enable SMB data integrity failover.

    Returns:
        {'id': '<job_id>'} — use get_failover_job_v1 to track progress.
    """
    params = _clean({
        "sourceid": sourceid,
        "targetid": targetid,
        "failovertarget": failovertarget,
        "pool": pool,
        "controlled": controlled,
        "datasync": datasync,
        "configsync": configsync,
        "resyncprep": resyncprep,
        "disablemirror": disablemirror,
        "quotasync": quotasync,
        "blockonwarnings": blockonwarnings,
        "rollbackrenameshares": rollbackrenameshares,
        "smbdataintegrity": smbdataintegrity,
    })
    return _post("/v1/jobs", params=params)


@_mcp_tool
def get_failover_job_v1(job_id: str) -> dict:
    """
    Retrieve a failover job by ID (v1 API).

    Args:
        job_id: Job ID returned by create_failover_job_v1.
    """
    return _get(f"/v1/jobs/{job_id}")


@_mcp_tool
def cancel_failover_job_v1(job_id: str) -> dict:
    """
    Cancel a running failover job (v1 API).

    Args:
        job_id: Job ID to cancel.
    """
    return _delete(f"/v1/jobs/{job_id}")


@_mcp_tool
def get_failover_job_log_v1(job_id: str) -> str:
    """
    Retrieve the log output for a failover job (v1 API).

    Args:
        job_id: Job ID.
    """
    return _get(f"/v1/jobs/{job_id}/log")


@_mcp_tool
def dr_test_mode_v1(
    policy: str,
    enable: bool,
    datasync: Optional[bool] = None,
    configsync: Optional[bool] = None,
) -> dict:
    """
    Enter or exit DR test mode for a policy (v1 API).

    Args:
        policy:     SyncIQ policy ID (from list_node_policies).
        enable:     True = enter DR test (make target writable).
                    False = exit DR test (make target read-only again).
        datasync:   Run data sync while entering DR test mode.
        configsync: Run config sync while entering DR test mode.

    Returns:
        {'id': '<job_id>'}
    """
    params = _clean({
        "policy": policy,
        "enable": enable,
        "datasync": datasync,
        "configsync": configsync,
    })
    return _post("/v1/jobs/drtest", params=params)


@_mcp_tool
def create_rehearsal_job_v1(
    sourceid: str,
    targetid: str,
    failovertarget: str,
    enable: bool,
    pool: Optional[str] = None,
) -> dict:
    """
    Create a rehearsal job (v1 API).

    Args:
        sourceid:       Source node ID.
        targetid:       Target node ID.
        failovertarget: Access zone ID or comma-separated policy IDs.
        enable:         True = enable rehearsal mode, False = disable.
        pool:           Pool name for pool-level rehearsal.

    Returns:
        {'id': '<job_id>'}
    """
    params = _clean({
        "sourceid": sourceid,
        "targetid": targetid,
        "failovertarget": failovertarget,
        "enable": enable,
        "pool": pool,
    })
    return _post("/v1/jobs/rehearsal", params=params)


# ══════════════════════════════════════════════════════════════════════════════
# FAILOVER JOBS (v2)
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_failover_jobs_v2(
    state: Optional[str] = None,
    success: Optional[bool] = None,
) -> list:
    """
    List failover jobs (v2 API).

    Args:
        state:   Filter: 'all' | 'running' | 'finished'. Default: all.
        success: Filter by result: True = successful, False = failed.
    """
    return _get("/v2/jobs/failover", params=_clean({"state": state, "success": success}))


@_mcp_tool
def create_failover_job_v2(
    sourceid: str,
    targetid: str,
    failovertarget: str,
    pool: Optional[str] = None,
    controlled: Optional[bool] = None,
    datasync: Optional[bool] = None,
    configsync: Optional[bool] = None,
    resyncprep: Optional[bool] = None,
    disablemirror: Optional[bool] = None,
    quotasync: Optional[bool] = None,
    blockonwarnings: bool = True,
    rollbackrenameshares: Optional[bool] = None,
    smbdataintegrity: Optional[bool] = None,
) -> dict:
    """
    Launch a new failover job (v2 API — recommended).

    Args:
        sourceid:            ID of the source (primary) node.
        targetid:            ID of the target (DR) node.
        failovertarget:      Access zone ID OR comma-separated SyncIQ policy IDs.
        pool:                Pool name for pool-level failover ('group:subnet:pool').
        controlled:          Controlled failover — operates on source AND target.
        datasync:            Run final incremental sync before failover.
        configsync:          Run configuration sync before failover.
        resyncprep:          Create mirror policies on source for future failback.
        disablemirror:       Disable mirror policies on target after failover.
        quotasync:           Fail over directory quotas to target.
        blockonwarnings:     Block the job if warnings are found (default True).
        rollbackrenameshares: Roll back renamed shares on failure.
        smbdataintegrity:    Enable SMB data integrity failover.

    Returns:
        {'id': '<job_id>'} — poll with get_failover_job_v2.
    """
    params = _clean({
        "sourceid": sourceid,
        "targetid": targetid,
        "failovertarget": failovertarget,
        "pool": pool,
        "controlled": controlled,
        "datasync": datasync,
        "configsync": configsync,
        "resyncprep": resyncprep,
        "disablemirror": disablemirror,
        "quotasync": quotasync,
        "blockonwarnings": blockonwarnings,
        "rollbackrenameshares": rollbackrenameshares,
        "smbdataintegrity": smbdataintegrity,
    })
    return _post("/v2/jobs/failover", params=params)


@_mcp_tool
def get_failover_job_v2(job_id: str) -> dict:
    """
    Retrieve a failover job by ID (v2 API).

    Args:
        job_id: Job ID returned by create_failover_job_v2.
    """
    return _get(f"/v2/jobs/failover/{job_id}")


@_mcp_tool
def cancel_failover_job_v2(job_id: str) -> dict:
    """
    Cancel a running failover job (v2 API).

    Args:
        job_id: Job ID to cancel.
    """
    return _delete(f"/v2/jobs/failover/{job_id}")


@_mcp_tool
def get_failover_job_log_v2(job_id: str) -> str:
    """
    Retrieve the log output for a failover job (v2 API).

    Args:
        job_id: Job ID.
    """
    return _get(f"/v2/jobs/failover/{job_id}/log")


@_mcp_tool
def dr_test_mode_v2(
    policy: str,
    enable: bool,
    datasync: Optional[bool] = None,
    configsync: Optional[bool] = None,
) -> dict:
    """
    Enter or exit DR test mode for a SyncIQ policy (v2 API).

    Args:
        policy:     SyncIQ policy ID.
        enable:     True = enter DR test (target writable).
                    False = exit DR test (target read-only).
        datasync:   Run data sync as part of DR test entry.
        configsync: Run config sync as part of DR test entry.

    Returns:
        {'id': '<job_id>'}
    """
    params = _clean({
        "policy": policy,
        "enable": enable,
        "datasync": datasync,
        "configsync": configsync,
    })
    return _post("/v2/jobs/failover/drtest", params=params)


@_mcp_tool
def create_rehearsal_job_v2(
    sourceid: str,
    targetid: str,
    failovertarget: str,
    enable: bool,
    pool: Optional[str] = None,
) -> dict:
    """
    Create or end a rehearsal job (v2 API).

    Args:
        sourceid:       Source node ID.
        targetid:       Target node ID.
        failovertarget: Access zone ID or comma-separated policy IDs.
        enable:         True = start rehearsal, False = end rehearsal.
        pool:           Pool name if doing a pool-level rehearsal.

    Returns:
        {'id': '<job_id>'}
    """
    params = _clean({
        "sourceid": sourceid,
        "targetid": targetid,
        "failovertarget": failovertarget,
        "enable": enable,
        "pool": pool,
    })
    return _post("/v2/jobs/failover/rehearsal", params=params)


# ══════════════════════════════════════════════════════════════════════════════
# READINESS JOBS (v2)
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_readiness_jobs() -> list:
    """List recent DR readiness assessment jobs."""
    return _get("/v2/jobs/readiness")


@_mcp_tool
def run_readiness_job() -> dict:
    """
    Run a new DR readiness job.

    Returns:
        {'id': '<job_id>'}
    """
    return _post("/v2/jobs/readiness")


@_mcp_tool
def get_readiness_job(job_id: str) -> dict:
    """
    Retrieve the results of a specific readiness job.

    Args:
        job_id: Job ID returned by run_readiness_job.
    """
    return _get(f"/v2/jobs/readiness/{job_id}")


# ══════════════════════════════════════════════════════════════════════════════
# REPLICATION JOBS (v2)
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_replication_jobs() -> list:
    """List recent configuration replication jobs."""
    return _get("/v2/jobs/replication")


@_mcp_tool
def run_replication_job() -> dict:
    """
    Run a configuration replication job immediately.

    Returns:
        {'id': '<job_id>'}
    """
    return _post("/v2/jobs/replication")


@_mcp_tool
def get_replication_job(job_id: str) -> dict:
    """
    Retrieve a specific replication job's details.

    Args:
        job_id: Job ID returned by run_replication_job.
    """
    return _get(f"/v2/jobs/replication/{job_id}")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION REPLICATION JOBS (v2) — per-node
# ══════════════════════════════════════════════════════════════════════════════

@_mcp_tool
def list_node_configrep_jobs(node_id: str) -> list:
    """
    List all configuration replication jobs for a specific node.

    Args:
        node_id: Node ID (from list_nodes).
    """
    return _get(f"/v2/nodes/{node_id}/configrep")


@_mcp_tool
def get_node_configrep_job(node_id: str, job_name: str) -> dict:
    """
    Get a specific configuration replication job on a node.

    Args:
        node_id:  Node ID.
        job_name: Config replication job name.
    """
    return _get(f"/v2/nodes/{node_id}/configrep/{job_name}")


@_mcp_tool
def update_node_configrep_job(
    node_id: str,
    job_name: str,
    enable: Optional[bool] = None,
    job_type: Optional[str] = None,
) -> dict:
    """
    Enable/disable or change the type of a configuration replication job.

    Args:
        node_id:  Node ID.
        job_name: Config replication job name.
        enable:   True = enable the job, False = disable.
        job_type: Job type — one of: 'AUTO', 'AUTODFS', 'AUTOSKIPCONFIG'.
    """
    return _put(f"/v2/nodes/{node_id}/configrep/{job_name}",
                params=_clean({"enable": enable, "type": job_type}))


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    transport = "stdio" if "--stdio" in sys.argv else "sse"
    # CLI --port overrides JSON config
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    else:
        port = MCP_PORT

    _log_path = Path(os.path.abspath(__file__)).parent / "superna_mcp.log"

    # Console banner — visible in the server's console window and the GUI
    # console panel (which streams server stdout via subprocess pipe).
    _nb_print("=" * 55)
    _nb_print(f"  Superna Eyeglass MCP Server  v{BUILD}")
    _nb_print(f"  Transport : {transport}")
    if transport == "sse":
        _nb_print(f"  SSE URL   : http://127.0.0.1:{port}/sse")
    _nb_print(f"  Eyeglass  : {EYEGLASS_HOST}  ssl={EYEGLASS_VERIFY_SSL}")
    _nb_print(f"  Log file  : {_log_path}")
    _nb_print(f"  Tools     : {len(_registered_tools)}")
    _nb_print("=" * 55)

    log.info("=" * 60)
    log.info("Superna MCP Server v%s starting  transport=%s  host=%s  port=%s",
             BUILD, transport, "127.0.0.1" if transport == "sse" else "n/a",
             port if transport == "sse" else "n/a")
    log.info("Eyeglass host: %s  verify_ssl=%s", EYEGLASS_HOST, EYEGLASS_VERIFY_SSL)
    log.info("Log file: %s", _log_path)
    log.info("Tools registered: %d  %s", len(_registered_tools), _registered_tools)

    try:
        if transport == "sse":
            mcp.settings.host = "127.0.0.1"
            mcp.settings.port = port
            mcp.run(transport="sse")
        else:
            mcp.run(transport="stdio")
    except Exception as exc:
        log.critical("Server crashed: %s\n%s", exc, traceback.format_exc())
        raise
