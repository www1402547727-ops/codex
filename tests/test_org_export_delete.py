from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import admin_console
import cloud_store
import platform_store
import teacher_registry


def _test_root() -> Path:
    requested = Path(
        os.environ.get("SCORE_TRACKER_TEST_ROOT", r"D:\gpt\workbuddy-cache")
    )
    try:
        requested.mkdir(parents=True, exist_ok=True)
        return requested
    except OSError:
        return Path(tempfile.mkdtemp(prefix="score_tracker_org_test_"))


def _make_db(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE students(
                id INTEGER PRIMARY KEY, name TEXT, grade TEXT
            );
            CREATE TABLE plans(
                id INTEGER PRIMARY KEY, plan_date TEXT, status TEXT
            );
            CREATE TABLE users(
                id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO students(id, name, grade) VALUES(1, ?, '测试年级')",
            (f"假学生{marker}",),
        )
        conn.execute(
            "INSERT INTO plans(id, plan_date, status) VALUES(1, '2030-01-01', '待上课')"
        )
        conn.execute(
            "INSERT INTO users(id, username, password_hash) VALUES(1, 'fake_user', 'fake-hash')"
        )
        conn.commit()
    finally:
        conn.close()


class OrganizationExportDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _test_root()

    def setUp(self):
        self.case_tmp = tempfile.TemporaryDirectory(dir=str(self.root))
        self.base = Path(self.case_tmp.name)
        self.teacher_root = self.base / "teachers"
        self.teacher_root.mkdir(parents=True, exist_ok=True)
        platform_store.configure(self.base)
        teacher_registry.configure(self.teacher_root)

    def tearDown(self):
        self.case_tmp.cleanup()

    def _org_with_members(self, name: str, slugs: list[str]) -> int:
        org_id, err = platform_store.create_organization(name)
        self.assertIsNotNone(org_id, err)
        for index, slug in enumerate(slugs):
            role = (
                platform_store.ROLE_ORG_ADMIN
                if index == 0
                else platform_store.ROLE_TEACHER
            )
            ok, member_err = platform_store.add_org_user(org_id, slug, role)
            self.assertTrue(ok, member_err)
            ok, teacher_err = teacher_registry.add_teacher(
                slug, f"显示名{slug}", slug
            )
            self.assertTrue(ok, teacher_err)
        return int(org_id)

    def test_export_contains_only_selected_org_and_each_table_csv(self):
        org_a = self._org_with_members("导出机构甲", ["org_a_admin", "org_a_teacher"])
        org_b = self._org_with_members("导出机构乙", ["org_b_admin"])
        ok, err = teacher_registry.add_teacher(
            "personal_teacher",
            "无机构老师",
            "personal_teacher",
            personal=True,
        )
        self.assertTrue(ok, err)
        dbs = {
            "org_a_admin": self.base / "a_admin" / "scores.db",
            "org_a_teacher": self.base / "a_teacher" / "scores.db",
            "org_b_admin": self.base / "b_admin" / "scores.db",
        }
        _make_db(dbs["org_a_admin"], "甲管理员")
        _make_db(dbs["org_a_teacher"], "甲老师")
        _make_db(dbs["org_b_admin"], "乙机构")

        data = admin_console.export_organization_zip(
            org_a,
            actor_slug="org_a_admin",
            include_photos=True,
            db_resolver=lambda slug: dbs[slug],
            photo_loader=lambda slug: [("答题卡.jpg", f"照片-{slug}".encode())],
        )

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            students_csv = archive.read(
                "老师/org_a_admin/students.csv"
            ).decode("utf-8-sig")
            plans_csv = archive.read("老师/org_a_admin/plans.csv").decode(
                "utf-8-sig"
            )
            users_csv = archive.read("老师/org_a_admin/users.csv").decode(
                "utf-8-sig"
            )

        self.assertIn("机构信息.csv", names)
        self.assertIn("老师清单.csv", names)
        self.assertIn("老师/org_a_admin/students.csv", names)
        self.assertIn("老师/org_a_admin/plans.csv", names)
        self.assertIn("老师/org_a_teacher/students.csv", names)
        self.assertIn("照片/org_a_admin/答题卡.jpg", names)
        self.assertIn("照片清单.csv", names)
        self.assertIn("假学生甲管理员", students_csv)
        self.assertIn("待上课", plans_csv)
        self.assertIn("[已隐藏]", users_csv)
        self.assertNotIn("fake-hash", users_csv)
        self.assertFalse(any("org_b_admin" in name for name in names))
        self.assertFalse(any("personal_teacher" in name for name in names))
        actions = {
            row["action"]
            for row in platform_store.list_audit(org_id=org_a)
        }
        self.assertIn("export", actions)
        self.assertNotEqual(org_a, org_b)

    def test_export_rejects_actor_from_another_org(self):
        org_a = self._org_with_members("导出边界甲", ["export_admin_a"])
        self._org_with_members("导出边界乙", ["export_admin_b"])
        db = self.base / "export_admin_a" / "scores.db"
        _make_db(db, "边界")
        with self.assertRaises(PermissionError):
            admin_console.export_organization_zip(
                org_a,
                actor_slug="export_admin_b",
                db_resolver=lambda _slug: db,
            )

    def test_delete_requires_exact_name_and_protects_platform_admin(self):
        admin_org = self._org_with_members("平台管理员所在机构", ["platform_admin"])
        teacher_registry.update_teacher("platform_admin", is_admin=True)
        ok, err = admin_console.delete_organization(
            admin_org,
            "平台管理员所在机构",
            actor_slug="platform_admin",
        )
        self.assertFalse(ok)
        self.assertIn("平台管理员", err)
        self.assertIsNotNone(platform_store.get_organization(admin_org))

        normal_org = self._org_with_members("普通待删机构", ["normal_admin"])
        ok, err = admin_console.delete_organization(
            normal_org,
            "名称不对",
            actor_slug="platform_admin",
        )
        self.assertFalse(ok)
        self.assertIn("名称", err)
        self.assertIsNotNone(platform_store.get_organization(normal_org))

    def test_delete_protects_former_admin_teacher_account(self):
        protected_org = self._org_with_members(
            "原教学数据所在机构",
            ["former_admin", "regular_teacher"],
        )
        teacher_registry.update_teacher("former_admin", protected=True)
        ok, err = teacher_registry.add_teacher(
            "platform_admin",
            "平台管理员",
            "platform_admin",
            is_admin=True,
        )
        self.assertTrue(ok, err)
        ok, err = admin_console.delete_organization(
            protected_org,
            "原教学数据所在机构",
            actor_slug="platform_admin",
        )
        self.assertFalse(ok)
        self.assertIn("受保护教学账号", err)
        self.assertIsNotNone(platform_store.get_organization(protected_org))

    def test_delete_removes_org_roster_storage_and_writes_audit(self):
        admin_org = self._org_with_members("平台总部", ["platform_admin"])
        teacher_registry.update_teacher("platform_admin", is_admin=True)
        org_id = self._org_with_members(
            "待删除机构", ["delete_admin", "delete_teacher"]
        )
        for slug in ("delete_admin", "delete_teacher"):
            _make_db(
                cloud_store.get_data_dir(self.teacher_root, slug) / "scores.db",
                slug,
            )

        with patch.object(
            cloud_store,
            "delete_teacher_storage",
            side_effect=[3, 4],
        ) as deleting:
            ok, err = admin_console.delete_organization(
                org_id,
                "待删除机构",
                actor_slug="platform_admin",
            )

        self.assertTrue(ok, err)
        self.assertIsNone(platform_store.get_organization(org_id))
        self.assertEqual(platform_store.list_org_users(org_id), [])
        self.assertIsNone(teacher_registry.teacher_entry("delete_admin"))
        self.assertIsNone(teacher_registry.teacher_entry("delete_teacher"))
        self.assertIsNotNone(teacher_registry.teacher_entry("platform_admin"))
        self.assertEqual(
            [call.args[0] for call in deleting.call_args_list],
            ["delete_admin", "delete_teacher"],
        )
        actions = [
            row["action"]
            for row in platform_store.list_audit(org_id=org_id)
        ]
        self.assertIn("delete_organization", actions)
        self.assertNotEqual(admin_org, org_id)

    def test_cloud_cleanup_uses_exact_files_and_never_platform_backup(self):
        config = {
            "url": "https://example.supabase.co",
            "key": "test-key",
            "bucket": "student-files",
        }

        def listing(prefix):
            rows = {
                "photos": [
                    {
                        "name": "legacy.jpg",
                        "object_path": "photos/legacy.jpg",
                        "is_file": True,
                    },
                    {
                        "name": "other_teacher",
                        "object_path": "photos/other_teacher",
                        "is_file": False,
                    },
                ],
                "photos/teacher_a": [
                    {
                        "name": "a.jpg",
                        "object_path": "photos/teacher_a/a.jpg",
                        "is_file": True,
                    }
                ],
                "backups": [
                    {
                        "name": "2026-10-28",
                        "object_path": "backups/2026-10-28",
                        "is_file": False,
                    }
                ],
                "backups/2026-10-28": [
                    {
                        "name": "scores.db",
                        "object_path": "backups/2026-10-28/scores.db",
                        "is_file": True,
                    },
                    {
                        "name": "platform.db",
                        "object_path": "backups/2026-10-28/platform.db",
                        "is_file": True,
                    },
                ],
                "backups/2026-10-28/teacher_a": [
                    {
                        "name": "scores.db",
                        "object_path": "backups/2026-10-28/teacher_a/scores.db",
                        "is_file": True,
                    }
                ],
            }
            return rows.get(prefix, [])

        with (
            patch.object(cloud_store, "_config", config),
            patch.object(cloud_store, "is_cloud", return_value=True),
            patch.object(
                cloud_store, "list_storage_objects", side_effect=listing
            ),
            patch.object(
                cloud_store, "delete_storage_objects", return_value=3
            ) as deleting,
        ):
            cloud_store.delete_teacher_storage("default")
            default_paths = deleting.call_args.args[0]
            cloud_store.delete_teacher_storage("teacher_a")
            teacher_paths = deleting.call_args.args[0]

        self.assertEqual(
            set(default_paths),
            {
                "database/scores.db",
                "photos/legacy.jpg",
                "backups/2026-10-28/scores.db",
            },
        )
        self.assertNotIn("backups/2026-10-28/platform.db", default_paths)
        self.assertEqual(
            set(teacher_paths),
            {
                "database/teacher_a/scores.db",
                "photos/teacher_a/a.jpg",
                "backups/2026-10-28/teacher_a/scores.db",
            },
        )


if __name__ == "__main__":
    unittest.main()
