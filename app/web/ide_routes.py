import os
import json
import sqlite3
import logging
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, g, session, abort
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

ide_bp = Blueprint("ide_routes", __name__)

workspace_path = Path.cwd().resolve()
logger.info(f"[IDE Bot] Active workspace: {workspace_path}")

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

# Flask API Web Routes
@ide_bp.route("/partner/developer/ide", methods=["GET"])
@partner_required
def ide_page():
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "ide_bot.html",
        active="today_dashboard",
        page_title="IDE Bot",
    )

@ide_bp.route("/api/ide/files", methods=["GET"])
@partner_required
def api_list_files():
    try:
        files = []
        list_files_recursive(workspace_path, files)
        return jsonify({"success": True, "files": files})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@ide_bp.route("/api/ide/file-content", methods=["GET"])
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

@ide_bp.route("/api/ide/save-file", methods=["POST"])
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

@ide_bp.route("/api/ide/chat", methods=["POST"])
@partner_required
def api_ide_chat():
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
        # Initialize Google GenAI client
        client = genai.Client(api_key=resolved_key)
        
        # System Instructions
        system_instruction = """You are a powerful, intelligent software developer assistant named "IDE Bot" created to manage, build, and debug applications. 
Your company is Cena's Kitchen (formerly Aguirre's Tex-Mex), website: https://cenaskitchen.com, app: https://app.cenaskitchen.com.
Here is key company information you MUST know and can speak about:
- Cuisine: Authentic Tex-Mex (fajitas, enchiladas, tacos, tostadas, house-made sauces)
- Locations: [{"city":"Houston","address":"15650 Farm to Market Road 529, Houston, TX 77095","phone":"(281) 815-3294"},{"city":"Tomball","address":"27727 Tomball Parkway, Tomball, TX 77377","phone":"(281) 255-0012"}]
- Settings: Edison-style lighting, festive atmosphere, Frida Kahlo inspired decorations
- App Features: ["Driver sign in and sign up","Real-time delivery tracking","Order dispatching and routing","Phone-based authentication flow"]
- Operational Summary: Cena's Kitchen serves high-quality Tex-Mex food in the Greater Houston and Tomball areas. They leverage technology through cenaskitchen.com (ordering and marketing) and app.cenaskitchen.com (their driver portal) to run efficient food delivery operations and provide a seamless customer experience.

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
- Because SQLite's built-in date/time functions do NOT recognize '+0000' as a valid timezone format, they will return NULL when parsing these fields directly.
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
  
Labor & Shift Database Details (Attached as toastdm):
- In addition to sales, you have a labor and shift database attached as 'toastdm' containing employee profiles, shifts, and schedules.
- Table 'toastdm.dm_profile' contains employee profile info. Schema:
  - cena_employee_id (INTEGER PRIMARY KEY)
  - full_name (TEXT)
  - active (INTEGER) - 1 if active, 0 otherwise
  - primary_store_key (TEXT) - 'copperfield' or 'tomball'
  - positions_json (TEXT, JSON array of canonical position names, e.g. '["Server"]', '["Cook"]')
- Table 'toastdm.dm_time_entry' contains clock-in / clock-out hours and earnings. Schema:
  - cena_employee_id (INTEGER)
  - store_key (TEXT) - 'copperfield' or 'tomball'
  - business_date (TEXT, YYYY-MM-DD format with hyphens!)
  - clock_in (TEXT, ISO timestamp)
  - clock_out (TEXT, ISO timestamp)
  - reg_hours (REAL) - regular hours worked
  - ot_hours (REAL) - overtime hours worked
  - total_hours (REAL) - reg_hours + ot_hours
  - base_pay (REAL) - actual labor cost (hourly_rate * reg_hours + hourly_rate * 1.5 * ot_hours)
  - tips (REAL)
- Table 'toastdm.dm_schedule' contains scheduled hours. Schema:
  - cena_employee_id (INTEGER)
  - shift_uid (TEXT)
  - store_key (TEXT) - 'copperfield' or 'tomball'
  - position_name (TEXT) - position name, e.g. 'Cook', 'Host', 'Cashier', 'Server', 'Busser', 'Bartender', 'Well', 'Prep', 'Expo', 'KM'
  - start_at (TEXT, ISO timestamp)
  - end_at (TEXT, ISO timestamp)
  - status (TEXT) - 'assigned' or 'open'

Common labor query formulations:
- To compare sales and labor by date, convert the format (e.g. replace(t.business_date, '-', '') to match YYYYMMDD sales date).
- Labor cost: SUM(base_pay) in toastdm.dm_time_entry
- Labor hours: SUM(total_hours) in toastdm.dm_time_entry
- Labor cost percentage: SUM(base_pay) from toastdm.dm_time_entry / SUM(amount) from toast_check_current * 100.0 (where voided=0 and deleted=0)
- Sales Per Labor Hour (SPLH): SUM(amount) from toast_check_current / SUM(total_hours) from toastdm.dm_time_entry
- BOH labor: Positions matching 'Cook', 'Prep', 'KM', 'Dish', 'Chop', 'Enchilada', 'Kitchen', 'Chef'
- FOH labor: Positions matching 'Server', 'Host', 'Cashier', 'Bartender', 'Well', 'Busser', 'Expo', 'Manager'
- Overtime checks: If SUM(ot_hours) > 0, overtime was run.
- Scheduled hours: SUM((strftime('%s', end_at) - strftime('%s', start_at)) / 3600.0) from toastdm.dm_schedule where status = 'assigned'
- To rank servers by net sales performance on a specific date, write an SQL query joining toast_check_current (c), toast_order_current (o), employee_toast_identity_map (m), and toastdm.dm_profile (p) on c.order_guid = o.order_guid, o.server_toast_guid = m.toast_employee_guid, and m.cena_employee_id = p.cena_employee_id. Group by p.full_name and order by net_sales desc.
- Shift definitions for servers/staff:
  - "tonight's shift" or "night shift" or "PM shift" refers to employees scheduled to start between 2:00 PM (14:00:00) and 11:00 PM (23:00:00) local time (e.g. time(s.start_at) >= '14:00:00' AND time(s.start_at) <= '23:00:00' in toastdm.dm_schedule).
  - "morning shift" or "AM shift" refers to employees scheduled to start between 7:00 AM (07:00:00) and 5:00 PM (17:00:00) local time (e.g. time(s.start_at) >= '07:00:00' AND time(s.start_at) <= '17:00:00' in toastdm.dm_schedule).
  - To identify who is scheduled for "tonight's shift" or the "morning shift" on a date and rank them, filter the server list by joining with toastdm.dm_schedule (s) on s.cena_employee_id = p.cena_employee_id where position_name = 'Server' and start_at date matches the query date (e.g., substr(s.start_at, 1, 10) = 'YYYY-MM-DD'), and apply these start time boundaries.

Rules of operation:
1. You have tools to read, write, and list files in the local workspace directory.
2. You have the 'query_sales_db_tool' tool to execute SQL queries on the real toast_webhook.sqlite database.
3. If the user asks restaurant operations, sales, average checks, covers, or order volume questions, ALWAYS use the 'query_sales_db_tool' tool to fetch the exact numbers from the real POS SQLite database tables (toast_check_current, toast_order_current, etc.). Formulate clean SQL queries using store_key and business_date. Do not guess.
4. If the user asks you to modify code, create files, or inspect files, ALWAYS use the appropriate tool: 'write_file_tool', 'read_file_tool', or 'list_files_tool'. Do not just say you will do it—DO IT.
5. Be professional, clean, and write pristine, correct code. Avoid placeholders or unfinished code blocks.
6. If asked about the company or the app (app.cenaskitchen.com), explain it clearly using the provided details.
"""

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
        
        tools_list = [list_files_tool, read_file_tool, write_file_tool, query_sales_db_tool]
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
                logger.info(f"[IDE Bot] Generation attempt with {candidate}")
                response = client.models.generate_content(
                    model=candidate,
                    contents=contents,
                    config=config
                )
                model_used = candidate
                success = True
                break
            except Exception as e:
                logger.warning(f"[IDE Bot] Candidate {candidate} failed: {e}")
                last_error = e
                
        if not success:
            return jsonify({
                "success": False,
                "error": f"All candidate models failed. Last error: {str(last_error)}"
            }), 500
            
        tools_executed = []
        
        # Tool Loop
        while response.function_calls:
            logger.info(f"[IDE Bot] Model {model_used} requested tool execution")
            
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
                        outcome = list_files_tool()
                    elif name == "read_file_tool":
                        outcome = read_file_tool(**args)
                    elif name == "write_file_tool":
                        outcome = write_file_tool(**args)
                    elif name == "query_sales_db_tool":
                        outcome = query_sales_db_tool(**args)
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
        logger.error(f"[IDE Bot] Chat processing failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
