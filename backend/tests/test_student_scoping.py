import io
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask
from pymongo.errors import DuplicateKeyError

from blueprints.admin import admin_bp
from blueprints.students import students_bp
import db


class StudentScopingTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = "test"
        app.register_blueprint(admin_bp)
        app.register_blueprint(students_bp)
        self.client = app.test_client()

    def login_as(self, role, username="staff"):
        with self.client.session_transaction() as session:
            session["user"] = {"username": username, "role": role}

    @patch("blueprints.students.db.list_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_teacher_without_filters_gets_full_assigned_class(self, get_scope, list_students):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        records = [{"student_id": "s1"}, {"student_id": "s2"}]
        get_scope.return_value = scope
        list_students.return_value = records
        self.login_as("teacher")

        response = self.client.get("/api/students")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"students": records, "count": 2})
        list_students.assert_called_once_with(filter_query=scope, limit=None)

    @patch("blueprints.students.db.list_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_search_is_combined_with_analyst_school_scope(self, get_scope, list_students):
        get_scope.return_value = {"school_id": "school-a"}
        list_students.return_value = []
        self.login_as("analyst")

        response = self.client.get("/api/students?search=Sam")

        self.assertEqual(response.status_code, 200)
        filter_query = list_students.call_args.kwargs["filter_query"]
        self.assertEqual(filter_query["$and"][0], {
            "$or": [
                {"student_id": {"$regex": "Sam", "$options": "i"}},
                {"full_name": {"$regex": "Sam", "$options": "i"}},
            ],
        })
        self.assertEqual(filter_query["$and"][1], {"school_id": "school-a"})
        list_students.assert_called_once_with(filter_query=filter_query, limit=None)

    @patch("blueprints.students.db.list_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_unassigned_staff_is_denied(self, get_scope, list_students):
        get_scope.return_value = None
        self.login_as("teacher")

        response = self.client.get("/api/students")

        self.assertEqual(response.status_code, 403)
        list_students.assert_not_called()

    @patch("blueprints.students.db.list_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_non_finite_range_filters_are_ignored(self, get_scope, list_students):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        records = [{"student_id": "s1"}]
        get_scope.return_value = scope
        list_students.return_value = records
        self.login_as("teacher")

        response = self.client.get(
            "/api/students?attendance_min=nan&gpa_min=inf&attendance_max=-inf"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"students": records, "count": 1})
        list_students.assert_called_once_with(filter_query=scope, limit=None)

    @patch("blueprints.students.db.list_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_finite_range_filters_still_apply(self, get_scope, list_students):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        get_scope.return_value = scope
        list_students.return_value = []
        self.login_as("teacher")

        response = self.client.get("/api/students?attendance_min=0.5&gpa_max=3.5")

        self.assertEqual(response.status_code, 200)
        list_students.assert_called_once_with(
            filter_query={"$and": [
                {"attendance_rate": {"$gte": 0.5}},
                {"previous_semester_gpa": {"$lte": 3.5}},
                scope,
            ]},
            limit=None,
        )

    @patch("blueprints.students.db.get_student", return_value=None)
    @patch("blueprints.students.db.student_scope_for_user")
    def test_individual_student_lookup_uses_staff_scope(self, get_scope, get_student):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        get_scope.return_value = scope
        self.login_as("teacher")

        response = self.client.get("/api/students/s1")

        self.assertEqual(response.status_code, 404)
        get_student.assert_called_once_with("s1", scope=scope)

    @patch("blueprints.students.db.delete_student", return_value={
        "predictions": 2,
        "alerts": 1,
        "goals": 1,
    })
    @patch("blueprints.students.db.student_scope_for_user")
    def test_teacher_delete_uses_assigned_scope(self, get_scope, delete_student):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        get_scope.return_value = scope
        self.login_as("teacher")

        response = self.client.delete("/api/students/s1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "message": "Deleted",
            "related_documents_deleted": {
                "predictions": 2,
                "alerts": 1,
                "goals": 1,
            },
        })
        delete_student.assert_called_once_with("s1", scope=scope)

    @patch("blueprints.students.db.delete_student", side_effect=PermissionError("outside scope"))
    @patch("blueprints.students.db.student_scope_for_user")
    def test_teacher_cannot_delete_student_outside_assigned_scope(
        self, get_scope, delete_student
    ):
        get_scope.return_value = {"school_id": "school-a", "class_id": "class-7"}
        self.login_as("teacher")

        response = self.client.delete("/api/students/s1")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json(), {"error": "outside scope"})

    @patch("blueprints.students.db.delete_student", return_value=False)
    def test_delete_missing_student_returns_not_found(self, delete_student):
        self.login_as("admin")

        response = self.client.delete("/api/students/s1")

        self.assertEqual(response.status_code, 404)
        delete_student.assert_called_once_with("s1", scope=None)

    @patch("blueprints.students.db.upsert_student")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_teacher_write_cannot_choose_another_scope(self, get_scope, upsert_student):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        get_scope.return_value = scope
        self.login_as("teacher")

        response = self.client.post("/api/students", json={
            "student_id": "s1",
            "school_id": "school-b",
            "class_id": "class-9",
        })

        self.assertEqual(response.status_code, 200)
        upsert_student.assert_called_once_with({
            "student_id": "s1",
            "school_id": "school-a",
            "class_id": "class-7",
        }, scope=scope)

    @patch("blueprints.students.db.bulk_upsert_students")
    @patch("blueprints.students.db.student_scope_for_user")
    def test_teacher_upload_assigns_scope_to_each_record(self, get_scope, bulk_upsert):
        scope = {"school_id": "school-a", "class_id": "class-7"}
        get_scope.return_value = scope
        bulk_upsert.return_value = (1, [])
        self.login_as("teacher")

        response = self.client.post("/api/data/ingest", json=[{
            "student_id": "s1",
            "school_id": "school-b",
            "class_id": "class-9",
        }])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["inserted"], 1)
        bulk_upsert.assert_called_once_with([{
            "student_id": "s1",
            "school_id": "school-a",
            "class_id": "class-7",
        }], scope=scope)

    @patch("db.students_col.bulk_write")
    def test_bulk_upsert_writes_in_batches_of_one_thousand(self, bulk_write):
        bulk_write.side_effect = [
            MagicMock(matched_count=1000, upserted_count=0),
            MagicMock(matched_count=1, upserted_count=0),
        ]
        records = [{"student_id": f"s{i}"} for i in range(1001)]

        inserted, errors = db.bulk_upsert_students(records)

        self.assertEqual(inserted, 1001)
        self.assertEqual(errors, [])
        self.assertEqual([len(call.args[0]) for call in bulk_write.call_args_list], [1000, 1])
        operation = bulk_write.call_args_list[0].args[0][0]
        self.assertEqual(operation._filter, {"student_id": "s0"})
        self.assertTrue(operation._upsert)

    @patch("blueprints.students.db.bulk_upsert_students", return_value=(1, []))
    def test_csv_ingest_converts_numeric_fields_and_preserves_row_errors(self, bulk_upsert):
        self.login_as("admin")

        response = self.client.post("/api/data/ingest", data={
            "file": (
                io.BytesIO(b"student_id,age\ns1,20\n,30\ns2,not-a-number\n"),
                "students.csv",
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "message": "Ingestion complete",
            "format": "csv",
            "inserted": 1,
            "skipped": 1,
            "errors": [{"row": 2, "error": "age must be a number"}],
        })
        bulk_upsert.assert_called_once_with(
            [{"student_id": "s1", "age": 20.0}],
            scope=None,
        )

    @patch("db.students_col.update_one")
    def test_scoped_upsert_targets_only_assigned_class(self, update_one):
        record = {
            "student_id": "s1",
            "school_id": "school-a",
            "class_id": "class-7",
        }
        scope = {"school_id": "school-a", "class_id": "class-7"}

        db.upsert_student(record, scope=scope)

        update_one.assert_called_once_with(
            {"student_id": "s1", **scope},
            {"$set": record},
            upsert=True,
        )

    @patch("db.students_col.update_one", side_effect=DuplicateKeyError("duplicate"))
    def test_scoped_upsert_rejects_existing_student_outside_class(self, update_one):
        record = {
            "student_id": "s1",
            "school_id": "school-a",
            "class_id": "class-7",
        }

        with self.assertRaises(PermissionError):
            db.upsert_student(record, scope={"school_id": "school-a", "class_id": "class-7"})

        update_one.assert_called_once()

    @patch("db.students_col.find_one", return_value=None)
    @patch("db.students_col.delete_one")
    def test_scoped_delete_targets_assigned_class(self, delete_one, find_one):
        delete_one.return_value.deleted_count = 1
        scope = {"school_id": "school-a", "class_id": "class-7"}

        with patch("db.predictions_col.delete_many") as delete_predictions, \
                patch("db.alerts_col.delete_many") as delete_alerts, \
                patch("db.goals_col.delete_many") as delete_goals:
            delete_predictions.return_value.deleted_count = 2
            delete_alerts.return_value.deleted_count = 3
            delete_goals.return_value.deleted_count = 1

            deleted = db.delete_student("s1", scope=scope)

        self.assertEqual(deleted, {
            "predictions": 2,
            "alerts": 3,
            "goals": 1,
        })
        delete_one.assert_called_once_with({"student_id": "s1", **scope})
        find_one.assert_not_called()
        delete_predictions.assert_called_once_with({"student_id": "s1"})
        delete_alerts.assert_called_once_with({"student_id": "s1"})
        delete_goals.assert_called_once_with({"student_id": "s1"})

    @patch("db.students_col.find_one", return_value={"student_id": "s1"})
    @patch("db.students_col.delete_one")
    def test_scoped_delete_rejects_student_outside_class(self, delete_one, find_one):
        delete_one.return_value.deleted_count = 0

        with patch("db.predictions_col.delete_many") as delete_predictions, \
                patch("db.alerts_col.delete_many") as delete_alerts, \
                patch("db.goals_col.delete_many") as delete_goals:
            with self.assertRaises(PermissionError):
                db.delete_student(
                    "s1",
                    scope={"school_id": "school-a", "class_id": "class-7"},
                )

        find_one.assert_called_once_with({"student_id": "s1"}, {"_id": 1})
        delete_predictions.assert_not_called()
        delete_alerts.assert_not_called()
        delete_goals.assert_not_called()

    @patch("db.students_col.find_one", return_value=None)
    @patch("db.students_col.delete_one")
    def test_scoped_delete_returns_false_for_missing_student(self, delete_one, find_one):
        delete_one.return_value.deleted_count = 0

        self.assertFalse(db.delete_student(
            "s1",
            scope={"school_id": "school-a", "class_id": "class-7"},
        ))
        find_one.assert_called_once()

    @patch("blueprints.admin.db.log_action")
    @patch("blueprints.admin.db.approve_user")
    @patch("blueprints.admin.db.users_col.find_one", return_value={
        "role": "teacher",
        "status": "approved",
    })
    def test_admin_can_assign_scope_to_existing_unassigned_teacher(
        self, find_one, approve_user, log_action
    ):
        self.login_as("admin")

        response = self.client.post(
            "/api/admin/approve/teacher-a",
            json={"school_id": "school-a", "class_id": "class-7"},
        )

        self.assertEqual(response.status_code, 200)
        approve_user.assert_called_once_with(
            "teacher-a",
            school_id="school-a",
            class_id="class-7",
        )
        log_action.assert_called_once_with(
            "staff",
            "admin",
            "assign-staff-scope",
            target="teacher-a",
        )
        find_one.assert_called_once()

    @patch("db.users_col.find")
    def test_pending_user_list_includes_approved_staff_missing_scope(self, find):
        find.return_value = [{
            "_id": "legacy-id",
            "username": "teacher-a",
            "role": "teacher",
            "status": "approved",
        }]

        users = db.list_pending_users()

        self.assertTrue(users[0]["needs_scope_assignment"])
        self.assertEqual(users[0]["_id"], "legacy-id")
        teacher_rule = next(
            rule for rule in find.call_args.args[0]["$or"]
            if rule.get("role") == "teacher"
        )
        self.assertEqual(teacher_rule["status"], "approved")
        self.assertIn({"class_id": {"$in": [None, ""]}}, teacher_rule["$or"])


if __name__ == "__main__":
    unittest.main()
