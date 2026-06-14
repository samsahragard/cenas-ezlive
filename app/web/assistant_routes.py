import os
import json
import sqlite3
import logging
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, g, session, abort
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

assistant_bp = Blueprint("assistant", __name__)

workspace_path = Path.cwd().resolve()
logger.info(f"[IDE Bot] Active workspace: {workspace_path}")

from app.services.assistant_routing_shared import (
    DATA_TOOL_RE as _DATA_TOOL_RE,
    DEFAULT_GEMINI_MODEL as _DEFAULT_GEMINI_MODEL,
    FOLLOWUP_RE as _FOLLOWUP_RE,
    MAX_QUESTION_CHARS as _MAX_QUESTION_CHARS,
    OPERATIONAL_NOUN_RE as _OPERATIONAL_NOUN_RE,
    REVIEW_STATUS as _REVIEW_STATUS,
    SECRET_DEFAULTS as _SECRET_DEFAULTS,
    SENSITIVE_RE as _SENSITIVE_RE,
    TOAST_DATA_FRESHNESS_RE as _TOAST_DATA_FRESHNESS_RE,
    TOAST_EMPLOYEE_PROFILE_RE as _TOAST_EMPLOYEE_PROFILE_RE,
    TOAST_SALES_RE as _TOAST_SALES_RE,
    TOAST_SALES_UNSUPPORTED_SCOPE_RE as _TOAST_SALES_UNSUPPORTED_SCOPE_RE,
    TOAST_TABLE_ACTIVITY_RE as _TOAST_TABLE_ACTIVITY_RE,
    TOAST_WEBHOOK_ACTIVITY_RE as _TOAST_WEBHOOK_ACTIVITY_RE,
    has_unsupported_toast_sales_scope as _has_unsupported_toast_sales_scope,
    now_iso as _now_iso,
    provider_timeout_ms as _provider_timeout_ms,
    queued_answer as _queued_answer,
    read_secret as _read_secret,
    requested_store as _requested_store,
    review_reason_label as _review_reason_label,
    review_risk_level as _review_risk_level,
    stable_hash as _stable_hash,
    toast_period_from_question as _toast_period_from_question,
    toast_table_business_date_from_question as _toast_table_business_date_from_question,
    today_ct as _today_ct,
    wants_cena_l3_business_analytics as _wants_cena_l3_business_analytics,
    wants_toast_data_freshness as _wants_toast_data_freshness,
    wants_toast_employee_profiles as _wants_toast_employee_profiles,
    wants_toast_sales_summary as _wants_toast_sales_summary,
    wants_toast_table_activity as _wants_toast_table_activity,
    wants_toast_webhook_activity as _wants_toast_webhook_activity,
)
# Gating decorator: only partner role with partner_auth_ok session can access
def partner_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("partner_auth_ok"):
            return redirect(url_for("auth.partner_login"))
        user = getattr(g, "current_user", None)
        if not (user is not None and getattr(user, "permission_level", None) == "partner"):
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

# Gating decorator: allow general logged-in users, but enforce second-factor for partners
def assistant_login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app.web.permissions import load_current_user
        user = getattr(g, "current_user", None) or load_current_user()
        if not user:
            return redirect(url_for("keypad_auth.login", next=request.path))
        if getattr(user, "permission_level", None) == "partner":
            if not session.get("partner_auth_ok"):
                return redirect(url_for("auth.partner_login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function

# Helper: Resolve cena_employee_id for the current user safely
def get_current_employee_id(user) -> int | None:
    if not user:
        return None
    # 1. Check main database for linked employee via user_id
    try:
        from app.db import SessionLocal
        from app.models import Employee
        db = SessionLocal()
        try:
            emp = db.query(Employee).filter(Employee.user_id == user.id).first()
            if emp:
                return emp.id
        except Exception:
            pass
        finally:
            db.close()
    except Exception:
        pass
    
    # 2. Fallback: Query toastdm.dm_profile inside sqlite file directly to avoid recursion
    try:
        toast_webhook_db = os.getenv("TOAST_WEBHOOK_DB") or r"C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite"
        cena_l3_data_dir = os.getenv("CENA_L3_DATA_DIR") or r"C:\Users\sam\cena-l3data"
        toastdm_db = Path(cena_l3_data_dir) / "snapshots" / "toastdm.sqlite"
        
        if os.path.exists(toast_webhook_db) and toastdm_db.exists():
            db_uri = f"file:{Path(toast_webhook_db).resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(db_uri, uri=True)
            try:
                tdm_uri = f"file:{toastdm_db.resolve().as_posix()}?mode=ro"
                conn.execute(f"ATTACH DATABASE '{tdm_uri}' AS toastdm")
                cursor = conn.cursor()
                cursor.execute("SELECT cena_employee_id FROM toastdm.dm_profile WHERE LOWER(full_name) = ? LIMIT 1", (user.full_name.lower(),))
                row = cursor.fetchone()
                if row:
                    return row[0]
            finally:
                conn.close()
    except Exception:
        pass
    return None

# Helper: Prevent directory traversal outside workspace
def safe_resolve(relative_path: str) -> Path:
    resolved = (workspace_path / relative_path).resolve()
    if not str(resolved).startswith(str(workspace_path)):
        raise PermissionError("Access denied: Path is outside the active workspace.")
    return resolved

# Helper: Recursively find workspace files
def list_files_recursive(dir_path: Path, file_list: list) -> list:
    for entry in dir_path.iterdir():
        # Ignore common control/cache/env directories
        if entry.name in ('.git', 'node_modules', '__pycache__', '.pytest_cache', '.env', '.venv', 'venv'):
            continue
        if entry.is_dir():
            list_files_recursive(entry, file_list)
        else:
            try:
                stat = entry.stat()
                rel_path = entry.relative_to(workspace_path).as_posix()
                file_list.append({
                    "path": rel_path,
                    "size": stat.st_size,
                    "mtime": int(stat.st_mtime * 1000)
                })
            except Exception:
                pass
    return file_list

# Tool Python Functions for Gemini to call
def list_files_tool() -> list[dict]:
    """
    Lists all files recursively inside the active workspace directory, ignoring node_modules, .git, and cache folders.
    """
    files = []
    list_files_recursive(workspace_path, files)
    return files

def read_file_tool(filePath: str) -> str:
    """
    Reads and returns the text contents of a file in the workspace.
    
    Args:
        filePath: The relative path of the file to read (e.g. "index.html" or "src/app.js").
    """
    resolved = safe_resolve(filePath)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"File not found: {filePath}")
    return resolved.read_text(encoding="utf-8")

def write_file_tool(filePath: str, content: str) -> str:
    """
    Creates a new file or overwrites an existing file with the provided text content.
    
    Args:
        filePath: The relative path of the file to write.
        content: The text content to be written.
    """
    resolved = safe_resolve(filePath)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return f"Successfully wrote {len(content)} characters to {filePath}"

def query_sales_db_tool(sqlQuery: str) -> list[dict]:
    """
    Runs an SQL SELECT query against the local SQLite restaurant sales database containing Toast check and order details.
    Tables: toast_check_current, toast_order_current, toast_selection_current.
    Also has toastdm database attached as toastdm containing: toastdm.dm_profile, toastdm.dm_time_entry, toastdm.dm_schedule.
    Also has toast database attached as toast_labor containing raw labor.
    store_key can be 'copperfield' or 'tomball'.
    business_date is stored as 'YYYYMMDD' string.
    Use clean local time conversion: time(replace(opened_date, '+0000', 'Z'), 'localtime').
    
    Args:
        sqlQuery: The SQL SELECT query to run.
    """
    # Gating and validation for SQL queries
    user = None
    try:
        from flask import has_request_context, g
        if has_request_context():
            user = getattr(g, "current_user", None)
    except Exception:
        pass

    if user is not None:
        user_role = getattr(user, "permission_level", "driver")
        manager_roles = {"corporate", "corporate_chef", "gm", "manager", "km", "assistant_km", "prep_manager", "foh_manager", "expo"}
        if user_role == "partner":
            user_tier = "partner"
        elif user_role in manager_roles:
            user_tier = "manager"
        else:
            user_tier = "hourly"
            
        if user_tier == "manager":
            query_upper = sqlQuery.upper()
            forbidden = ["TOAST_LABOR", "BASE_PAY", "TIPS", "SQLITE_MASTER", "SQLITE_SCHEMA", "SQLITE_TEMP_MASTER", "PRAGMA"]
            for word in forbidden:
                if word in query_upper:
                    raise PermissionError(f"Access denied: Query contains restricted table or field '{word}' for managers.")
            if "DM_TIME_ENTRY" in query_upper and "*" in query_upper:
                raise PermissionError("Access denied: SELECT * is not permitted on 'dm_time_entry' to protect individual pay details. Select explicit non-pay columns like cena_employee_id, clock_in, clock_out.")
                    
        elif user_tier == "hourly":
            my_emp_id = get_current_employee_id(user)
            if not my_emp_id:
                raise PermissionError("Access denied: Could not map user to employee identity.")
                
            query_upper = sqlQuery.upper()
            # 1. No sales tables allowed
            sales_tables = ["TOAST_CHECK_CURRENT", "TOAST_ORDER_CURRENT", "TOAST_SELECTION_CURRENT", "TOAST_PAYMENT_CURRENT"]
            for t in sales_tables:
                if t in query_upper:
                    raise PermissionError(f"Access denied: Hourly staff is not permitted to query sales table {t}.")
            
            # 2. Must filter by their own employee ID
            if "CENA_EMPLOYEE_ID" not in query_upper:
                raise PermissionError("Access denied: Hourly staff queries must filter on cena_employee_id.")
            
            if str(my_emp_id) not in sqlQuery:
                raise PermissionError("Access denied: Hourly staff can only query their own records.")

    toast_webhook_db = os.getenv("TOAST_WEBHOOK_DB") or r"C:\Users\sam\cena-ai-assistant\toast_webhook\toast_webhook.sqlite"
    is_render = os.getenv("RENDER") == "true"
    
    if is_render or not os.path.exists(toast_webhook_db):
        import urllib.request
        import urllib.error
        import base64
        import json
        
        cloud_url = os.getenv("AI_ASSISTANT_CK_RUNTIME_URL") or "https://cena-cloud.onrender.com"
        cloud_token = os.getenv("AI_ASSISTANT_CK_RUNTIME_TOKEN") or os.getenv("CENA_CLOUD_TOKEN") or ""
        
        if "/assistant/answer" in cloud_url:
            base_url = cloud_url.split("/assistant/answer")[0]
        else:
            base_url = cloud_url.rstrip("/")
            
        endpoint = base_url + "/sync/query_db"
        
        payload = json.dumps({"sqlQuery": sqlQuery}).encode("utf-8")
        req = urllib.request.Request(endpoint, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        
        auth_str = f"sam:{cloud_token}"
        encoded_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", f"Basic {encoded_auth}")
        
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("success"):
                    return data.get("results") or []
                else:
                    raise Exception(data.get("error") or "Unknown error from cloud DB proxy")
        except urllib.error.HTTPError as exc:
            try:
                err_content = exc.read().decode("utf-8")
                err_data = json.loads(err_content)
                err_msg = err_data.get("error") or str(exc)
            except Exception:
                err_msg = str(exc)
            raise Exception(f"Cloud DB proxy HTTP error {exc.code}: {err_msg}")
        except Exception as exc:
            raise Exception(f"Cloud DB proxy connection failed: {exc}")

    cena_l3_data_dir = os.getenv("CENA_L3_DATA_DIR") or r"C:\Users\sam\cena-l3data"
    snapshots_dir = Path(cena_l3_data_dir) / "snapshots"
    
    toastdm_db = snapshots_dir / "toastdm.sqlite"
    toast_db = snapshots_dir / "toast.sqlite"
    
    # Open SQLite read-only connection
    db_uri = f"file:{Path(toast_webhook_db).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Attach snapshot databases read-only
        if toastdm_db.exists():
            tdm_uri = f"file:{toastdm_db.resolve().as_posix()}?mode=ro"
            conn.execute(f"ATTACH DATABASE '{tdm_uri}' AS toastdm")
        if toast_db.exists():
            tst_uri = f"file:{toast_db.resolve().as_posix()}?mode=ro"
            conn.execute(f"ATTACH DATABASE '{tst_uri}' AS toast_labor")
            
        cursor = conn.cursor()
        cursor.execute(sqlQuery)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def fetch_toast_live_data_tool(dataType: str, location: str) -> dict | list:
    """
    Fetches real-time operational data directly from the Toast API for a given location, bypassing the SQLite database replica.
    Only Partners and Managers have access to this tool.
    
    Args:
        dataType: The type of live data to fetch. Must be one of:
                  - 'tables': Open tables, active checks, assigned servers, check numbers, open items, and amounts so far.
                  - 'clockins': Currently clocked-in staff, their positions, clock-in times, and regular/overtime hours so far (excluding pay rate/tips).
                  - 'sales': Today's live sales summary (check counts, closed vs open checks, net sales closed, total guests) and top items rung in today.
        location: The location/store key to fetch data for. Must be 'tomball' or 'copperfield'.
    """
    # Gating check
    user = None
    try:
        from flask import has_request_context, g
        if has_request_context():
            user = getattr(g, "current_user", None)
    except Exception:
        pass

    if user is not None:
        user_role = getattr(user, "permission_level", "driver")
        manager_roles = {"corporate", "corporate_chef", "gm", "manager", "km", "assistant_km", "prep_manager", "foh_manager", "expo"}
        if user_role == "partner":
            user_tier = "partner"
        elif user_role in manager_roles:
            user_tier = "manager"
        else:
            user_tier = "hourly"
            
        if user_tier == "hourly":
            raise PermissionError("Access denied: Hourly employees are not permitted to fetch Toast live data.")

    loc = str(location or "").lower().strip()
    if loc not in ("tomball", "copperfield"):
        raise ValueError("Invalid location. Must be 'tomball' or 'copperfield'.")

    from app.services.toast_client import ToastClient, restaurant_guids
    guids = restaurant_guids()
    restaurant_guid = guids.get(loc)
    if not restaurant_guid:
        raise ValueError(f"No Toast restaurant GUID configured for location: {location}")

    toast = ToastClient.shared()

    from app.services.assistant_routing_shared import today_ct
    bd_dt = today_ct()
    bd = bd_dt.strftime("%Y%m%d")

    if dataType == "tables":
        from app.services.toast_table_activity import _table_name_map, _employee_name_map, _table_guid, _table_name_from_order, _ref_guid, _ref_name, _parse_iso, _format_ct
        
        table_map, _ = _table_name_map(toast, loc, restaurant_guid)
        employee_map, _ = _employee_name_map(toast, loc, restaurant_guid, refresh=True)
        orders = toast.fetch_orders_for_date(loc, restaurant_guid, bd, refresh=True)

        open_checks = []
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            if order.get("voided") or order.get("deleted"):
                continue
            
            table_guid = _table_guid(order)
            table_name = _table_name_from_order(order)
            if table_guid:
                table_name = table_map.get(table_guid) or table_name

            for check in order.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                if check.get("voided") or check.get("deleted"):
                    continue
                
                closed_date = check.get("closedDate")
                if closed_date:
                    continue

                opened_at_raw = check.get("openedDate") or order.get("openedDate")
                opened_at_str = ""
                if opened_at_raw:
                    opened_at = _parse_iso(opened_at_raw)
                    if opened_at:
                        opened_at_str = _format_ct(opened_at)

                server_guid = _ref_guid(check.get("server") or order.get("server"))
                server_name = employee_map.get(server_guid) if server_guid else None
                if not server_name:
                    server_name = _ref_name(check.get("server") or order.get("server"), employee_map) or "Unknown Server"

                items_rung_in = []
                for sel in check.get("selections") or []:
                    if not isinstance(sel, dict):
                        continue
                    if sel.get("voided") or sel.get("deleted"):
                        continue
                    items_rung_in.append({
                        "name": sel.get("displayName") or sel.get("name") or "Unknown Item",
                        "quantity": sel.get("quantity") or 1,
                        "price": sel.get("price") or 0.0,
                        "amount": sel.get("netAmount") or sel.get("amount") or 0.0
                    })

                open_checks.append({
                    "table_name": table_name or "Unknown Table",
                    "check_number": check.get("displayNumber") or order.get("displayNumber") or "Unknown Check",
                    "assigned_server": server_name,
                    "opened_at": opened_at_str,
                    "amount_so_far": check.get("amount") or check.get("totalAmount") or 0.0,
                    "items_rung_in": items_rung_in
                })
        return open_checks

    elif dataType == "clockins":
        from app.services.toast_table_activity import _employee_name_map, _parse_iso, _format_ct
        
        start = datetime(bd_dt.year, bd_dt.month, bd_dt.day, 0, 0, 0)
        end = datetime(bd_dt.year, bd_dt.month, bd_dt.day, 23, 59, 59)
        time_entries = toast.fetch_time_entries(loc, restaurant_guid, start, end, refresh=True)
        employee_map, _ = _employee_name_map(toast, loc, restaurant_guid, refresh=True)

        jobs = toast.fetch_jobs(loc, restaurant_guid)
        job_map = {j["guid"]: (j.get("title") or "?").strip() for j in jobs or [] if isinstance(j, dict) and "guid" in j}

        clockins = []
        for te in time_entries or []:
            if not isinstance(te, dict):
                continue
            if te.get("deleted"):
                continue

            out_date = te.get("outDate")
            is_open = out_date in (None, "") or (out_date or "").startswith("1970")
            if not is_open:
                continue

            emp_guid = (te.get("employeeReference") or {}).get("guid")
            emp_name = employee_map.get(emp_guid) if emp_guid else "Unknown Employee"

            job_guid = (te.get("jobReference") or {}).get("guid")
            position = job_map.get(job_guid) or "Unknown Position"

            in_date_raw = te.get("inDate")
            clock_in_str = ""
            if in_date_raw:
                in_dt = _parse_iso(in_date_raw)
                if in_dt:
                    clock_in_str = _format_ct(in_dt)

            reg_hours = float(te.get("regularHours") or 0.0)
            ot_hours = float(te.get("overtimeHours") or 0.0)

            clockins.append({
                "employee_name": emp_name,
                "position": position,
                "clock_in_time": clock_in_str,
                "regular_hours": reg_hours,
                "overtime_hours": ot_hours
            })
        return clockins

    elif dataType == "sales":
        orders = toast.fetch_orders_for_date(loc, restaurant_guid, bd, refresh=True)

        closed_count = 0
        open_count = 0
        net_sales_closed = 0.0
        total_guests = 0
        item_counts = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            if order.get("voided") or order.get("deleted"):
                continue

            for check in order.get("checks") or []:
                if not isinstance(check, dict):
                    continue
                if check.get("voided") or check.get("deleted"):
                    continue

                closed_date = check.get("closedDate")
                is_closed = bool(closed_date)

                amt = float(check.get("amount") or check.get("totalAmount") or 0.0)
                guests = int(check.get("customerCount") or check.get("guestCount") or order.get("customerCount") or order.get("guestCount") or 0)
                total_guests += guests

                if is_closed:
                    closed_count += 1
                    net_sales_closed += amt
                else:
                    open_count += 1

                for sel in check.get("selections") or []:
                    if not isinstance(sel, dict):
                        continue
                    if sel.get("voided") or sel.get("deleted"):
                        continue
                    name = sel.get("displayName") or sel.get("name") or "Unknown Item"
                    qty = float(sel.get("quantity") or 1.0)
                    item_counts[name] = item_counts.get(name, 0.0) + qty

        top_items = sorted(
            [{"name": k, "quantity": v} for k, v in item_counts.items()],
            key=lambda x: x["quantity"],
            reverse=True
        )[:5]

        return {
            "check_counts": closed_count + open_count,
            "closed_checks": closed_count,
            "open_checks": open_count,
            "net_sales_closed": round(net_sales_closed, 2),
            "total_guests": total_guests,
            "top_items_rung_in": top_items
        }

    else:
        raise ValueError("Invalid dataType. Must be 'tables', 'clockins', or 'sales'.")

# Flask API Web Routes
@assistant_bp.route("/assistant", methods=["GET"])
@assistant_login_required
def assistant_page():
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "assistant_page.html",
        active="assistant_page",
        page_title="Cenas AI",
    )

@assistant_bp.route("/api/assistant/files", methods=["GET"])
@partner_required
def api_list_files():
    try:
        files = []
        list_files_recursive(workspace_path, files)
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@assistant_bp.route("/api/assistant/file-content", methods=["GET"])
@partner_required
def api_file_content():
    relative_path = request.args.get("path")
    if not relative_path:
        return jsonify({"success": False, "error": "Path is required"}), 400
    try:
        content = read_file_tool(relative_path)
        return jsonify({"success": True, "content": content})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@assistant_bp.route("/api/assistant/save-file", methods=["POST"])
@partner_required
def api_save_file():
    body = request.get_json(silent=True) or {}
    relative_path = body.get("path")
    content = body.get("content")
    if not relative_path or content is None:
        return jsonify({"success": False, "error": "Path and content are required"}), 400
    try:
        msg = write_file_tool(relative_path, content)
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@assistant_bp.route("/api/assistant/chat", methods=["POST"])
@assistant_login_required
def api_assistant_chat():
    body = request.get_json(silent=True) or {}
    message = body.get("message")
    history = body.get("history") or []
    apiKey = body.get("apiKey")
    
    # Resolve API Key
    resolved_key = apiKey or os.getenv("GEMINI_API_KEY")
    if not resolved_key:
        return jsonify({
            "success": False,
            "error": "Gemini API Key is missing. Please configure it in Settings or the server environment."
        }), 400
        
    try:
        user = getattr(g, "current_user", None)
        if not user:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
            
        user_name = getattr(user, "full_name", "User")
        user_role = getattr(user, "permission_level", "staff")
        
        # Determine User Tier
        manager_roles = {"corporate", "corporate_chef", "gm", "manager", "km", "assistant_km", "prep_manager", "foh_manager", "expo"}
        if user_role == "partner":
            user_tier = "partner"
        elif user_role in manager_roles:
            user_tier = "manager"
        else:
            user_tier = "hourly"
            
        # Dynamically build tools list based on tier
        if user_tier == "partner":
            tools_list = [list_files_tool, read_file_tool, write_file_tool, query_sales_db_tool, fetch_toast_live_data_tool]
        elif user_tier == "manager":
            tools_list = [query_sales_db_tool, fetch_toast_live_data_tool]
        else:
            tools_list = [query_sales_db_tool]
            
        # Resolve Employee ID for hourly gating
        my_emp_id = None
        if user_tier == "hourly":
            my_emp_id = get_current_employee_id(user)
            
        # Base System Instructions
        base_instruction = f"""You are CENA, the intelligent and premium AI assistant for Cena's Kitchen.
Your company is Cena's Kitchen (formerly Aguirre's Tex-Mex), website: https://cenaskitchen.com, app: https://app.cenaskitchen.com.
Here is key company information you MUST know and can speak about:
- Cuisine: Authentic Tex-Mex (fajitas, enchiladas, tacos, tostadas, house-made sauces)
- Locations: [{{"city":"Houston","address":"15650 Farm to Market Road 529, Houston, TX 77095","phone":"(281) 815-3294"}},{{"city":"Tomball","address":"27727 Tomball Parkway, Tomball, TX 77377","phone":"(281) 255-0012"}}]
- Settings: Edison-style lighting, festive atmosphere, Frida Kahlo inspired decorations
- App Features: ["Driver sign in and sign up","Real-time delivery tracking","Order dispatching and routing","Phone-based authentication flow"]
- Operational Summary: Cena's Kitchen serves high-quality Tex-Mex food in the Greater Houston and Tomball areas. They leverage technology through cenaskitchen.com (ordering and marketing) and app.cenaskitchen.com (their driver portal) to run efficient food delivery operations.

Sales Database Details (toast_webhook.sqlite):
- Business Week: Cena's Kitchen's business week starts on Sunday and ends on Saturday. Therefore, from the perspective of current time (Sunday, June 14, 2026), "last week" is Sunday, June 7, 2026 to Saturday, June 13, 2026 (inclusive). Keep this week boundary in mind for all weekly calculations.
- You have a SQLite database containing real POS transaction data for Cena's Kitchen.
- The locations are identified by the 'store_key' field: 'copperfield' and 'tomball'.
- Dates are stored in YYYYMMDD string format (e.g. '20260605' for June 5, 2026) in the 'business_date' field.
- The principal table for sales is 'toast_check_current'. Schema:
  - check_guid (TEXT PRIMARY KEY)
  - order_guid (TEXT)
  - store_key (TEXT) - 'copperfield' or 'tomball'
  - business_date (TEXT, YYYYMMDD format)
  - amount (REAL) - net sales amount for this check
  - total_amount (REAL) - gross sales amount for this check
  - tax_amount (REAL)
  - opened_date (TEXT, ISO timestamp)
  - closed_date (TEXT, ISO timestamp)
  - paid_date (TEXT, ISO timestamp)
  - voided (INTEGER) - 1 if voided, 0 otherwise
  - deleted (INTEGER) - 1 if deleted, 0 otherwise
- To calculate check counts, use: COUNT(check_guid)
- To calculate total net sales, use: SUM(amount) where voided = 0 AND deleted = 0
- To calculate total gross sales, use: SUM(total_amount) where voided = 0 AND deleted = 0
- To calculate average check, use: AVG(total_amount) or AVG(amount)
- IMPORTANT: Time fields (opened_date, closed_date, paid_date) are stored in UTC format with a '+0000' offset suffix (e.g., '2026-06-05T16:09:43.351+0000').
- You MUST clean the timezone format in your queries by replacing '+0000' with 'Z' (e.g., replace(opened_date, '+0000', 'Z')).
- The restaurant operates in Houston (local time: America/Chicago, which is UTC-5 hours for CDT and UTC-6 hours for CST).
- To query or group by local time (e.g., local hours for lunch/dinner dayparts), you MUST convert the cleaned UTC timestamp to local time using the 'localtime' modifier:
  - Extract local time string: time(replace(opened_date, '+0000', 'Z'), 'localtime')
  - Lunch daypart checks (local time before 16:00:00): time(replace(opened_date, '+0000', 'Z'), 'localtime') < '16:00:00'
  - Dinner daypart checks (local time between 16:00:00 and 22:00:00): time(replace(opened_date, '+0000', 'Z'), 'localtime') >= '16:00:00' AND time(replace(opened_date, '+0000', 'Z'), 'localtime') < '22:00:00'
- Table 'toast_order_current' contains order headers. Schema:
  - order_guid (TEXT PRIMARY KEY)
  - store_key (TEXT)
  - business_date (TEXT)
  - server_toast_guid (TEXT) - unique Toast GUID of the server
- Table 'employee_toast_identity_map' maps Toast server GUIDs to corporate employee IDs. Schema:
  - toast_employee_guid (TEXT) - unique Toast server GUID (matches server_toast_guid)
  - cena_employee_id (INTEGER) - corporate employee ID (matches toastdm.dm_profile.cena_employee_id)
- Table 'toast_selection_current' contains items ordered. Schema:
  - selection_guid (TEXT PRIMARY KEY)
  - check_guid (TEXT)
  - order_guid (TEXT)
  - store_key (TEXT)
  - business_date (TEXT)
  - display_name (TEXT) - menu item name
  - quantity (REAL)
  - price (REAL)
  - voided (INTEGER)
- Table 'toast_dimension_item' contains metadata dimensions (like table numbers). Schema:
  - domain (TEXT) - 'table' for table information
  - store_key (TEXT) - 'copperfield' or 'tomball'
  - toast_guid (TEXT) - unique Toast GUID of the table (matches order_current.table_guid)
  - name (TEXT) - clean table number or name (e.g. '91', '101')
  
Labor & Shift Database Details (Attached as toastdm):
- In addition to sales, you have a labor and shift database attached as 'toastdm' containing employee profiles, shifts, and schedules.
- Table 'toastdm.dm_profile' contains employee profile info. Schema:
  - cena_employee_id (INTEGER PRIMARY KEY)
  - full_name (TEXT)
  - active (INTEGER) - 1 if active, 0 otherwise
  - primary_store_key (TEXT) - 'copperfield' or 'tomball'
  - positions_json (TEXT, JSON array of canonical position names, e.g. '["Server"]', '["Cook"]')
- Table 'toastdm.dm_schedule' contains scheduled hours. Schema:
  - cena_employee_id (INTEGER)
  - shift_uid (TEXT)
  - store_key (TEXT) - 'copperfield' or 'tomball'
  - position_name (TEXT) - position name, e.g. 'Cook', 'Host', 'Cashier', 'Server', 'Busser', 'Bartender', 'Well', 'Prep', 'Expo', 'KM'
  - start_at (TEXT, ISO timestamp)
  - end_at (TEXT, ISO timestamp)
  - status (TEXT) - 'assigned' or 'open'

Shift definitions for servers/staff:
- "tonight's shift", "tonight", or "PM shift" without a specified date refers to the night shift on the CURRENT date (Sunday, June 14, 2026).
- If referencing a past date (e.g. June 10th), "night shift", "PM shift", or "that night" refers to the PM shift on that specific past date.
- "night shift" or "PM shift" refers to employees whose scheduled hours fall within the 2:00 PM to 11:00 PM window (specifically: time(s.start_at) >= '14:00:00' AND time(s.end_at) <= '23:00:00' in toastdm.dm_schedule).
- "morning shift", "morning staff", or "AM shift" refers to employees whose scheduled hours fall within the 7:00 AM to 5:00 PM window (specifically: time(s.start_at) >= '07:00:00' AND time(s.end_at) <= '17:00:00' in toastdm.dm_schedule).
- To identify who is scheduled for a shift on a date and rank them, filter the employee list by joining with toastdm.dm_schedule (s) on s.cena_employee_id = p.cena_employee_id where position_name IN ('Server', 'Bartender') and start_at date matches the query date (e.g., substr(s.start_at, 1, 10) = 'YYYY-MM-DD'), and apply the shift boundaries.
- IMPORTANT performance ranking rules for tipped employees (Waiters/Servers and Bartenders): When asked who the "better", "best", "strongest", "weakest", or "worse" waiters (servers) or bartenders are (historically or for an active/today/tonight/future shift), their performance MUST be evaluated and ranked based on their historical credit card tips and transactions over the last 30 days (excluding today).
  Evaluate them based on these metrics directly calculated from the transaction database:
  1. Tip % (Primary Performance Indicator for "who is better"): calculated as: (SUM(pay.tip_amount) / SUM(pay.amount)) * 100.0 from toast_payment_current pay where pay.payment_type = 'CREDIT' AND pay.payment_status = 'CAPTURED'.
  2. CC Tabs (Sales/Tab Volume): SUM(pay.amount) for CREDIT payments.
  3. CC Tips (Total Tips): SUM(pay.tip_amount) for CREDIT payments.
  4. Tickets (Ticket/Check Volume): COUNT(DISTINCT c.check_guid).
  5. Avg Duration (Table turn time): AVG(strftime('%s', replace(c.closed_date, '+0000', 'Z')) - strftime('%s', replace(c.opened_date, '+0000', 'Z'))) / 60.0 in minutes.
  - When asked to rank scheduled FOH tipped employees, write an SQL query to calculate these metrics over the last 30 days (excluding today) for each scheduled employee, present the results sorted by Tip % descending in a markdown table, and write a summary explaining who is performing better (higher tip percentage and/or volume) and who is not. Do not use today's partial sales.

Live Operations, Open Tables, Clocked-in Staff, and Live Sales/Store Queries:
- IMPORTANT: For any query about "right now", "currently", "live", or "today" regarding open tables, clocked-in staff, waitstaff/cooks active, items rung in today, guest counts today, or live/today's sales summary, you MUST use the `fetch_toast_live_data_tool` instead of querying the sales/labor database.
- Use `fetch_toast_live_data_tool` with `dataType='tables'` to get open tables, active checks, assigned servers, check numbers, open items, and amounts so far.
- Use `fetch_toast_live_data_tool` with `dataType='clockins'` to get currently clocked-in staff, their positions, clock-in times, and regular/overtime hours so far today.
- Use `fetch_toast_live_data_tool` with `dataType='sales'` to get today's live sales summary (check counts, closed vs open checks, net sales closed, total guests) and top items rung in.
- This tool bypasses the database and routes live data directly from the Toast API.
"""

        if user_tier == "partner":
            system_instruction = base_instruction + f"""
IDENTITY VERIFICATION & GATING RULES:
- You are speaking to {user_name}, who is logged in as a 'partner'.
- On the first turn of a conversation, you MUST explicitly greet {user_name} by name, state/verify their role as 'partner', and confirm they have full access.
- As a partner, they have full access to everything: codebase files, database schemas, raw SQL queries, hourly pay rates, and labor costs.
- You have access to files tools (list_files_tool, read_file_tool, write_file_tool), query_sales_db_tool, and fetch_toast_live_data_tool.
- In addition to sales, you have access to the 'toastdm.dm_time_entry' table and the 'toast_labor' database to query labor costs, hourly pay rates, and hours worked.
"""
        elif user_tier == "manager":
            system_instruction = base_instruction + f"""
IDENTITY VERIFICATION & GATING RULES:
- You are speaking to {user_name}, who is logged in as a 'manager' (role: {user_role}).
- On the first turn of a conversation, you MUST explicitly greet {user_name} by name, state/verify their role as '{user_role}', and remind them of their access limits.
- CRITICAL SECURITY ENFORCEMENT: Since they are NOT a partner, you are strictly prohibited from giving them access to:
  1. Workspace/codebase files (you do NOT have files tools, and must never discuss or print code files or paths).
  2. Database schemas, raw SQL queries, or internal table/column structures. Never explain SQL queries or print them.
  3. Hourly pay rates (individual hourly pay) or total labor costs (labor cost sum, hourly rate avg, base_pay, or total labor spend).
- If they ask for any codebase, file, schema, raw SQL, or labor cost/pay rate information, you MUST politely refuse, stating that you have verified their role as '{user_role}' and that such information is restricted to Partners only.
- You have query_sales_db_tool and fetch_toast_live_data_tool. You MUST NOT query labor costs or hourly pay rates, but you can fetch live operational data via fetch_toast_live_data_tool.
- Do NOT output SQL query text or database names under any circumstance to this user.
"""
        else:  # hourly
            system_instruction = base_instruction + f"""
IDENTITY VERIFICATION & GATING RULES:
- You are speaking to {user_name}, who is logged in as a '{user_role}' (hourly/tipped employee).
- On the first turn of a conversation, you MUST explicitly greet {user_name} by name, state/verify their role as '{user_role}', and confirm they only have access to their own personal records.
- CRITICAL SECURITY ENFORCEMENT: Since they are an hourly/tipped employee, they are strictly prohibited from receiving ANY company/store-wide information, sales data, schedules of other employees, database schemas, raw SQL, or files.
- They can ONLY access their own personal schedule and profile information.
- You have ONLY the query_sales_db_tool. You MUST NOT query any sales tables or other employees' records. Any query you make MUST filter on cena_employee_id = {my_emp_id} (their employee ID).
- If they ask for any company information, sales, other people's schedules, codebase, or schemas, you MUST politely refuse, stating that you have verified their identity as '{user_name}' ('{user_role}') and that company/manager information is restricted.
"""

        # Initialize Google GenAI client
        client = genai.Client(api_key=resolved_key)

        # Format history
        chat_history = []
        for turn in history:
            role = "model" if turn.get("role") == "assistant" else "user"
            chat_history.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=turn.get("content", ""))]
                )
            )
            
        contents = list(chat_history)
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=message)]
            )
        )
        
        # Candidate models list
        model_candidates = [
            os.getenv("GEMINI_MODEL"),
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-1.5-flash"
        ]
        model_candidates = [m for m in model_candidates if m]
        
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools_list,
        )
        
        # Generation loop
        success = False
        last_error = None
        response = None
        model_used = None
        
        for candidate in model_candidates:
            try:
                logger.info(f"[CENA] Generation attempt with {candidate}")
                response = client.models.generate_content(
                    model=candidate,
                    contents=contents,
                    config=config
                )
                model_used = candidate
                success = True
                break
            except Exception as e:
                logger.warning(f"[CENA] Candidate {candidate} failed: {e}")
                last_error = e
                
        if not success:
            return jsonify({
                "success": False,
                "error": f"All candidate models failed. Last error: {str(last_error)}"
            }), 500
            
        tools_executed = []
        
        # Tool Loop
        while response.function_calls:
            logger.info(f"[CENA] Model {model_used} requested tool execution")
            
            # Save model call turn
            contents.append(response.candidates[0].content)
            
            tool_parts = []
            for call in response.function_calls:
                name = call.name
                args = call.args or {}
                
                success_run = True
                outcome = None
                try:
                    if name == "list_files_tool":
                        if user_tier != "partner":
                            raise PermissionError("Access denied: list_files_tool is restricted to partners.")
                        outcome = list_files_tool()
                    elif name == "read_file_tool":
                        if user_tier != "partner":
                            raise PermissionError("Access denied: read_file_tool is restricted to partners.")
                        outcome = read_file_tool(**args)
                    elif name == "write_file_tool":
                        if user_tier != "partner":
                            raise PermissionError("Access denied: write_file_tool is restricted to partners.")
                        outcome = write_file_tool(**args)
                    elif name == "query_sales_db_tool":
                        outcome = query_sales_db_tool(**args)
                    elif name == "fetch_toast_live_data_tool":
                        if user_tier not in ("partner", "manager"):
                            raise PermissionError("Access denied: fetch_toast_live_data_tool is restricted to partners and managers.")
                        outcome = fetch_toast_live_data_tool(**args)
                    else:
                        raise ValueError(f"Unknown tool name: {name}")
                except Exception as err:
                    success_run = False
                    outcome = str(err)
                    
                # Format for frontend logs
                outcome_str = json.dumps(outcome)[:100] + "..." if isinstance(outcome, (dict, list)) else str(outcome)
                tools_executed.append({
                    "name": name.replace("_tool", ""),
                    "args": args,
                    "success": success_run,
                    "outcome": outcome_str
                })
                
                # Part response
                part = types.Part.from_function_response(
                    name=name,
                    response={"result": outcome}
                )
                tool_parts.append(part)
                
            contents.append(types.Content(role="tool", parts=tool_parts))
            
            # Next turn
            response = client.models.generate_content(
                model=model_used,
                contents=contents,
                config=config
            )
            
        return jsonify({
            "success": True,
            "text": response.text,
            "toolsExecuted": tools_executed
        })
        
    except Exception as e:
        logger.error(f"[CENA] Chat processing failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

import re
from typing import Any

_L3_DATA_QUESTION_RE = re.compile(
    r"\b(sales|net|gross|revenue|orders?|catering|labor|hours?|overtime|\bot\b|"
    r"avg|average|check|covers?|drivers?|deliver\w*|items?|sold|menu|store|"
    r"copperfield|tomball|week|weekly|day|daypart|month|april|may|march|june|"
    r"anomal\w*|trend|compare|comparison|why|how many|how much|busiest|slowest|"
    r"top|best|worst|highest|lowest|splh|prime cost|spend)\b",
    re.IGNORECASE,
)

def _l3_investigation_answer(question: str) -> dict[str, Any] | None:
    """Local/dev L3 path: investigate a data question that matched no tool.
    Defensive - any failure returns None so the conversational fallback runs."""
    if not _L3_DATA_QUESTION_RE.search(question or ""):
        return None
    try:
        from app.services.cena_sql_orchestrator import answer_question
        res = answer_question(question)
    except Exception:  # noqa: BLE001
        logger.exception("assistant: L3 investigation failed")
        return None
    if not isinstance(res, dict) or not res.get("ok") or not str(res.get("answer", "")).strip():
        return None
    return res
