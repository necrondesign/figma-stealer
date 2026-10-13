"""Ссылка на папку Figma: разбор ссылки, список файлов папки и добавление их в проект."""

import json
import tempfile
import unittest
from pathlib import Path

from coloro import db as dbm
from coloro import rules, server
from coloro.figma import Figma, FigmaError


class FakeFolders:
    """Поддельная Figma для папок: отдаёт заданный список файлов."""

    def __init__(self, folders):
        self.folders = folders
        self.calls = []

    def folder_files(self, folder_id):
        self.calls.append(folder_id)
        if folder_id not in self.folders:
            raise FigmaError("not_found", "Folder not found. Check the folder link and access.", 404)
        name, keys = self.folders[folder_id]
        return {"name": name, "files": [{"key": k, "name": f"File {k}", "thumbnail_url": "", "last_modified": ""}
                                        for k in keys]}


class Answers(Figma):
    """Настоящий клиент, у которого вместо сети — ответы по адресам; чего нет, то 404."""

    def __init__(self, answers):
        super().__init__("x" * 40)
        self.answers = answers
        self.asked = []

    def get_json(self, path, params=None, api="v1"):
        self.asked.append(f"/{api}{path}")
        got = self.answers.get(f"/{api}{path}", 404)
        if got == 403:
            raise FigmaError("forbidden", "Access denied. Check the file URL and the token permissions.", 403)
        if got == 404:
            raise FigmaError("not_found", "File not found. Check the file URL and access.", 404)
        return got


class FolderLinks(unittest.TestCase):
    def test_folder_links(self):
        for url in ("https://www.figma.com/files/folder/12345678",
                    "https://www.figma.com/files/team/1111/folder/12345678",
                    "https://www.figma.com/files/team/1111/folder/12345678/Name?fuid=5",
                    "https://www.figma.com/files/project/12345678",
                    "https://www.figma.com/files/team/1111/project/12345678/Mobile-app",
                    "https://figma.com/files/2222/project/12345678"):
            self.assertEqual(rules.parse_folder_link(url), "12345678", url)

    def test_not_folder_links(self):
        for url in ("https://www.figma.com/design/AbC123/Name?node-id=1-2",
                    "https://www.figma.com/design/AbC123/project",
                    "https://www.figma.com/files/team/1111",
                    "https://www.figma.com/files/folder/abc",
                    "https://example.com/files/folder/12345678",
                    "AbC123", ""):
            self.assertIsNone(rules.parse_folder_link(url), url)


class FolderFiles(unittest.TestCase):
    LIST = {"name": "Mobile", "files": [{"key": "K1", "name": "One"}]}

    def test_v2_first(self):
        f = Answers({"/v2/folders/7/files": self.LIST})
        self.assertEqual(f.folder_files("7"), self.LIST)
        self.assertEqual(f.asked, ["/v2/folders/7/files"])

    def test_old_projects_endpoint_as_fallback(self):
        f = Answers({"/v2/folders/7/files": 403, "/v1/projects/7/files": self.LIST})
        self.assertEqual(f.folder_files("7"), self.LIST)

    def test_token_without_folders_read(self):
        f = Answers({"/v2/folders/7/files": 403, "/v1/projects/7/files": 403})
        with self.assertRaises(FigmaError) as e:
            f.folder_files("7")
        self.assertEqual(e.exception.code, "no_folder_scope")
        self.assertIn("Folders: read", str(e.exception))

    def test_unknown_folder(self):
        with self.assertRaises(FigmaError) as e:
            Answers({}).folder_files("7")
        self.assertEqual(e.exception.code, "not_found")
        self.assertIn("Folder", str(e.exception))


class AddFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = dbm.connect(Path(self.tmp.name) / "t.sqlite")
        self.pid = server.create_project(self.con, "App")["id"]
        self.figma = FakeFolders({"12345678": ("Mobile", ["KA", "KB", "KC"])})

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def keys(self, pid=None):
        return [r[0] for r in self.con.execute("SELECT file_key FROM sources WHERE project_id = ? ORDER BY id",
                                               (pid or self.pid,))]

    def test_folder_adds_every_file(self):
        r = server.add_source(self.con, "https://www.figma.com/files/folder/12345678", ["stage"], self.pid, self.figma)
        self.assertEqual(r, {"folder": "Mobile", "added": 3, "files": 3})
        self.assertEqual(self.keys(), ["KA", "KB", "KC"])
        pages = {p for (p,) in self.con.execute("SELECT pages FROM sources")}
        self.assertEqual(pages, {json.dumps(["stage"])})

    def test_files_already_in_the_project_are_skipped(self):
        server.add_source(self.con, "https://www.figma.com/design/KB/Name?node-id=1-2", None, self.pid)
        r = server.add_source(self.con, "https://www.figma.com/files/team/1/folder/12345678", None, self.pid, self.figma)
        self.assertEqual(r["added"], 2)
        self.assertEqual(self.keys(), ["KB", "KA", "KC"])
        again = server.add_source(self.con, "https://www.figma.com/files/folder/12345678", None, self.pid, self.figma)
        self.assertEqual(again["added"], 0)
        self.assertEqual(len(self.keys()), 3)

    def test_other_project_does_not_count(self):
        other = server.create_project(self.con, "Site")["id"]
        server.add_source(self.con, "https://www.figma.com/files/folder/12345678", None, other, self.figma)
        r = server.add_source(self.con, "https://www.figma.com/files/folder/12345678", None, self.pid, self.figma)
        self.assertEqual(r["added"], 3)

    def test_figma_error_reaches_the_person(self):
        with self.assertRaises(FigmaError):
            server.add_source(self.con, "https://www.figma.com/files/folder/999", None, self.pid, self.figma)
        self.assertEqual(self.keys(), [])

    def test_file_link_still_works(self):
        r = server.add_source(self.con, "https://www.figma.com/design/KA/x", None, self.pid, self.figma)
        self.assertEqual(r, {"file_key": "KA"})
        self.assertEqual(self.figma.calls, [])


if __name__ == "__main__":
    unittest.main()
