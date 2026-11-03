import io
import re
import csv
import json
import math
from flask import Blueprint, request, jsonify, session
import db
from extensions import login_required

students_bp = Blueprint("students", __name__, url_prefix="/api")


# List/search the student roster.
@students_bp.route("/students", methods=["GET"])
@login_required(roles=["admin", "teacher", "analyst"])
def get_students():
    search_query = (request.args.get("search") or request.args.get("query") or "").strip()
    conditions = []
    user = session["user"]
    scope = db.student_scope_for_user(user["username"], user["role"])
    if scope is None:
        return jsonify({"error": "Your account is not assigned to a school or class"}), 403

    if search_query:
        # Escape before using as a Mongo $regex to avoid injection/ReDoS.
        safe_pattern = re.escape(search_query[:100])  # also cap length
        conditions.append({"$or": [
            {"student_id": {"$regex": safe_pattern, "$options": "i"}},
            {"full_name": {"$regex": safe_pattern, "$options": "i"}},
        ]})

    # Advanced filters — attendance/GPA range and risk level, all optional.
    def _num(name):
        val = request.args.get(name)
        try:
            num = float(val) if val not in (None, "") else None
        except ValueError:
            return None
        return num if num is None or math.isfinite(num) else None

    attendance_min, attendance_max = _num("attendance_min"), _num("attendance_max")
    gpa_min, gpa_max = _num("gpa_min"), _num("gpa_max")
    risk = request.args.get("risk")  # "high" | "safe"

    if attendance_min is not None or attendance_max is not None:
        rng = {}
        if attendance_min is not None:
            rng["$gte"] = attendance_min
        if attendance_max is not None:
            rng["$lte"] = attendance_max
        conditions.append({"attendance_rate": rng})

    if gpa_min is not None or gpa_max is not None:
        rng = {}
        if gpa_min is not None:
            rng["$gte"] = gpa_min
        if gpa_max is not None:
            rng["$lte"] = gpa_max
        conditions.append({"previous_semester_gpa": rng})

    if risk in ("high", "safe"):
        conditions.append({"dropout_risk": 1 if risk == "high" else 0})

    if scope:
        conditions.append(scope)

    if conditions:
        filter_query = {"$and": conditions} if len(conditions) > 1 else conditions[0]
        students = db.list_students(filter_query=filter_query, limit=None)
    else:
        students = db.list_students(limit=10)
    return jsonify({"students": students, "count": len(students)}), 200


@students_bp.route("/cohort/compare", methods=["GET"])
@login_required(roles=["admin", "analyst"])
def compare_cohorts():
    """Aggregate stats for two filtered student groups, side by side.
    Filters: ?group_a_risk=high&group_b_risk=safe (also *_gender)."""
    def build_filter(prefix):
        conditions = []
        risk = request.args.get(f"{prefix}_risk")
        if risk in ("high", "safe"):
            conditions.append({"dropout_risk": 1 if risk == "high" else 0})
        gender = request.args.get(f"{prefix}_gender")
        if gender:
            conditions.append({"gender": gender})
        return {"$and": conditions} if conditions else {}

    comparison = db.compare_student_cohorts(
        build_filter("group_a"),
        build_filter("group_b"),
    )
    return jsonify(comparison), 200


@students_bp.route("/students/<student_id>", methods=["GET"])
@login_required(roles=["admin", "teacher", "analyst", "student"])
def get_student(student_id):
    user = session["user"]
    if user["role"] == "student" and user["username"] != student_id:
        return jsonify({"error": "Students may only view their own record"}), 403
    scope = {}
    if user["role"] in ("teacher", "analyst"):
        scope = db.student_scope_for_user(user["username"], user["role"])
        if scope is None:
            return jsonify({"error": "Your account is not assigned to a school or class"}), 403
    student = db.get_student(student_id, scope=scope)
    if not student:
        return jsonify({"error": "Not found"}), 404
    return jsonify(student), 200


@students_bp.route("/students/<student_id>", methods=["DELETE"])
@login_required(roles=["admin", "teacher"])
def delete_student(student_id):
    user = session["user"]
    scope = {}
    if user["role"] == "teacher":
        scope = db.student_scope_for_user(user["username"], user["role"])
        if scope is None:
            return jsonify({"error": "Your account is not assigned to a school or class"}), 403
    try:
        deleted = db.delete_student(student_id, scope=scope or None)
    except PermissionError as error:
        return jsonify({"error": str(error)}), 403
    if not deleted:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "message": "Deleted",
        "related_documents_deleted": deleted,
    }), 200


@students_bp.route("/students", methods=["POST"])
@login_required(roles=["admin", "teacher"])
def upsert_student():
    user = session["user"]
    data = request.get_json(force=True)
    scope = {}
    if user["role"] == "teacher":
        scope = db.student_scope_for_user(user["username"], user["role"])
        if scope is None:
            return jsonify({"error": "Your account is not assigned to a school or class"}), 403
        if isinstance(data, dict):
            data = {**data, **scope}
    try:
        db.upsert_student(data, scope=scope or None)
    except PermissionError as error:
        return jsonify({"error": str(error)}), 403
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"message": "Saved"}), 200


@students_bp.route("/data/ingest", methods=["POST"])
@login_required(roles=["admin", "teacher"])
def ingest_csv():
    """Batch ingest via CSV (multipart, field 'file') or JSON
    (raw array, {"students": [...]}, or a .json file upload)."""
    user = session["user"]
    scope = {}
    if user["role"] == "teacher":
        scope = db.student_scope_for_user(user["username"], user["role"])
        if scope is None:
            return jsonify({"error": "Your account is not assigned to a school or class"}), 403

    numeric_fields = {
        field for field, expected_type in db.STUDENT_FIELDS.items()
        if expected_type is not str
    }

    def ingest_rows(rows, csv_input=False):
        skipped, errors = 0, []
        prepared_docs, prepared_rows = [], []
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append({"row": i, "error": "Student record must be an object"})
                continue
            row = dict(row)
            if scope:
                row.update(scope)
            if row.get("student_id") in (None, ""):
                skipped += 1
                continue
            try:
                if csv_input:
                    for numeric_field in numeric_fields:
                        if row.get(numeric_field) == "":
                            row.pop(numeric_field)
                        elif row.get(numeric_field) is not None:
                            try:
                                row[numeric_field] = float(row[numeric_field])
                            except (TypeError, ValueError) as error:
                                raise ValueError(f"{numeric_field} must be a number") from error
                db.validate_student(row)
                prepared_docs.append(row)
                prepared_rows.append(i)
            except PermissionError as error:
                errors.append({"row": i, "error": str(error)})
            except ValueError as error:
                errors.append({"row": i, "error": str(error)})

        inserted = 0
        if prepared_docs:
            inserted, bulk_errors = db.bulk_upsert_students(
                prepared_docs,
                scope=scope or None,
            )
            errors.extend(
                {"row": prepared_rows[index], "error": message}
                for index, message in bulk_errors
            )
        errors.sort(key=lambda error: error["row"])
        return inserted, skipped, errors

    def rows_from_payload(payload):
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("students")
        else:
            rows = None
        if not isinstance(rows, list) or not rows:
            return None
        return rows

    # JSON body (no file) — e.g. { "students": [ {...}, {...} ] }
    if request.is_json:
        payload = request.get_json(force=True)
        rows = rows_from_payload(payload)
        if rows is None:
            return jsonify({"error": "Expected a JSON array or {\"students\": [...]}"}), 400
        inserted, skipped, errors = ingest_rows(rows)
        return jsonify({"message": "Ingestion complete", "format": "json", "inserted": inserted, "skipped": skipped, "errors": errors[:10]}), 200

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded (expected form field 'file', or a JSON body)"}), 400

    file = request.files["file"]
    filename = (file.filename or "").lower()

    if filename.endswith(".json"):
        try:
            payload = json.loads(file.stream.read().decode("utf-8"))
        except Exception:
            return jsonify({"error": "Invalid JSON file"}), 400
        rows = rows_from_payload(payload)
        if rows is None:
            return jsonify({"error": "Expected a JSON array or {\"students\": [...]}"}), 400
        inserted, skipped, errors = ingest_rows(rows)
        return jsonify({"message": "Ingestion complete", "format": "json", "inserted": inserted, "skipped": skipped, "errors": errors[:10]}), 200

    if filename.endswith(".csv"):
        stream = io.StringIO(file.stream.read().decode("utf-8"))
        reader = csv.DictReader(stream)
        unknown_headers = set(reader.fieldnames or []) - db.STUDENT_FIELDS.keys()
        if unknown_headers:
            fields = ", ".join(sorted(unknown_headers, key=str))
            return jsonify({"error": f"Unsupported student field(s): {fields}"}), 400
        inserted, skipped, errors = ingest_rows(list(reader), csv_input=True)
        return jsonify({"message": "Ingestion complete", "format": "csv", "inserted": inserted, "skipped": skipped, "errors": errors[:10]}), 200

    return jsonify({"error": "Only .csv or .json files are supported"}), 400
