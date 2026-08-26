import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).with_name("whart_tools.py")
MODULE_SPEC = importlib.util.spec_from_file_location("whart_tools_under_test", MODULE_PATH)
whart_tools = importlib.util.module_from_spec(MODULE_SPEC)
assert MODULE_SPEC and MODULE_SPEC.loader
if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *args, **kwargs: None
    requests_stub.post = lambda *args, **kwargs: None
    requests_stub.patch = lambda *args, **kwargs: None
    requests_stub.delete = lambda *args, **kwargs: None
    sys.modules["requests"] = requests_stub
MODULE_SPEC.loader.exec_module(whart_tools)


class _DummyResponse:
    status_code = 200
    headers = {}
    text = ""
    content = b""

    def raise_for_status(self):
        return None


class _JsonResponse(_DummyResponse):
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self.text = ""
        self.content = b"{}"

    def json(self):
        return self.payload


class _BinaryResponse(_DummyResponse):
    def __init__(self, payload: bytes, headers=None):
        self.payload = payload
        self.headers = headers or {}
        self.text = payload.decode("utf-8", errors="replace")
        self.content = payload

    def iter_content(self, chunk_size=8192):
        yield self.payload


class WhartToolsScreenshotResolutionTests(unittest.TestCase):
    def _write_file(self, file_path: str):
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(b"fake-png")

    @patch.object(whart_tools.requests, "post", return_value=_DummyResponse())
    def test_upload_screenshot_falls_back_to_tmp_screenshots(self, mock_post):
        with tempfile.TemporaryDirectory() as temp_root:
            screenshot_dir = os.path.join(temp_root, "skill_runtime", "screenshots", "3", "163")
            tmpdir = os.path.join(temp_root, "tmp")
            fallback_file = os.path.join(tmpdir, "screenshots", "step1.png")
            self._write_file(fallback_file)

            with patch.dict(
                os.environ,
                {"SCREENSHOT_DIR": screenshot_dir, "TMPDIR": tmpdir},
                clear=False,
            ):
                result = whart_tools.upload_screenshot(3, 163, "step1.png", "登录截图")

        self.assertEqual(result, {"message": "截图 '登录截图' 上传成功"})
        mock_post.assert_called_once()

    @patch.object(whart_tools.requests, "post", return_value=_DummyResponse())
    def test_upload_screenshots_falls_back_by_basename_for_missing_screen_dir_path(self, mock_post):
        with tempfile.TemporaryDirectory() as temp_root:
            screenshot_dir = os.path.join(temp_root, "skill_runtime", "screenshots", "3", "163")
            os.makedirs(screenshot_dir, exist_ok=True)
            tmpdir = os.path.join(temp_root, "tmp")
            self._write_file(os.path.join(tmpdir, "screenshots", "step1.png"))
            self._write_file(os.path.join(tmpdir, "screenshots", "step2.png"))

            stale_path_1 = os.path.join(screenshot_dir, "step1.png")
            stale_path_2 = os.path.join(screenshot_dir, "step2.png")
            with patch.dict(
                os.environ,
                {"SCREENSHOT_DIR": screenshot_dir, "TMPDIR": tmpdir},
                clear=False,
            ):
                result = whart_tools.upload_screenshots(
                    3,
                    163,
                    f"{stale_path_1},{stale_path_2}",
                    "登录功能测试截图",
                )

        self.assertEqual(result, {"message": "成功上传 2 张截图"})
        mock_post.assert_called_once()

    def test_upload_screenshot_missing_error_lists_searched_dirs(self):
        with tempfile.TemporaryDirectory() as temp_root:
            screenshot_dir = os.path.join(temp_root, "skill_runtime", "screenshots", "3", "163")
            tmpdir = os.path.join(temp_root, "tmp")

            with patch.dict(
                os.environ,
                {"SCREENSHOT_DIR": screenshot_dir, "TMPDIR": tmpdir},
                clear=False,
            ):
                result = whart_tools.upload_screenshot(3, 163, "missing.png", "缺失截图")

        self.assertIn("文件不存在: missing.png", result["error"])
        self.assertIn("已搜索目录", result["error"])
        self.assertIn(screenshot_dir, result["error"])
        self.assertIn(os.path.join(tmpdir, "screenshots"), result["error"])

    @patch.object(whart_tools.requests, "post", return_value=_DummyResponse())
    def test_upload_screenshot_falls_back_to_recent_file_when_name_mismatch(self, mock_post):
        with tempfile.TemporaryDirectory() as temp_root:
            screenshot_dir = os.path.join(temp_root, "skill_runtime", "screenshots", "1", "1520")
            actual_file = os.path.join(screenshot_dir, "case_1520_step1.png")
            self._write_file(actual_file)

            with patch.dict(
                os.environ,
                {"SCREENSHOT_DIR": screenshot_dir},
                clear=False,
            ):
                result = whart_tools.upload_screenshot(
                    1, 1520, "步骤1_登录成功.png", "步骤1 登录成功"
                )

        self.assertEqual(result, {"message": "截图 '步骤1 登录成功' 上传成功"})
        mock_post.assert_called_once()

    @patch.object(whart_tools.requests, "post", return_value=_DummyResponse())
    def test_upload_screenshot_does_not_fallback_to_last_png_for_unrelated_name(self, mock_post):
        with tempfile.TemporaryDirectory() as temp_root:
            screenshot_dir = os.path.join(temp_root, "skill_runtime", "screenshots", "1", "1523")
            self._write_file(os.path.join(screenshot_dir, "last.png"))
            self._write_file(os.path.join(screenshot_dir, "case_1523_step1.png"))

            with patch.dict(
                os.environ,
                {"SCREENSHOT_DIR": screenshot_dir},
                clear=False,
            ):
                result = whart_tools.upload_screenshot(
                    1, 1523, "步骤5_点击文件.png", "步骤5 点击文件"
                )

        self.assertIn("文件不存在", result["error"])
        mock_post.assert_not_called()

    @patch.object(whart_tools.requests, "get")
    def test_get_testcases_returns_api_total_and_case_ids(self, mock_get):
        response = MagicMock()
        response.json.return_value = {
            "total": 51,
            "data": [
                {"id": 457, "name": "Case A"},
                {"id": 458, "name": "Case B"},
            ],
        }
        mock_get.return_value = response

        result = whart_tools.get_testcases(1, 32)

        self.assertEqual(result["total"], 51)
        self.assertEqual(result["returned_count"], 2)
        self.assertEqual(result["case_id_range"], [457, 458])
        self.assertEqual(result["case_ids"], [457, 458])
        self.assertIn("共有 51 条用例", result["authoritative_summary"])
        self.assertIn("Use total", result["counting_rule"])

    @patch.object(whart_tools.requests, "post")
    def test_add_testcase_converts_literal_newline_sequences(self, mock_post):
        response = MagicMock()
        response.json.return_value = {"code": 201, "data": {"id": 1, "name": "Case"}}
        mock_post.return_value = response

        whart_tools.add_testcase(
            1,
            1,
            "Case",
            precondition="First line\\nSecond line",
            notes="Source\\r\\nReview complete",
        )

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["precondition"], "First line\nSecond line")
        self.assertEqual(payload["notes"], "Source\nReview complete")


class WhartToolsAddModuleTests(unittest.TestCase):
    @patch.object(whart_tools.requests, "post")
    def test_add_module_success(self, mock_post):
        class MockResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "code": 201,
                    "data": {
                        "id": 42,
                        "name": "新模块"
                    }
                }
        mock_post.return_value = MockResponse()

        result = whart_tools.add_module(project_id=1, name="新模块", parent_id=10)
        self.assertEqual(result["message"], "保存成功")
        self.assertEqual(result["module"]["id"], 42)
        self.assertEqual(result["module"]["name"], "新模块")

        mock_post.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/testcase-modules/",
            headers=whart_tools.HEADERS,
            json={"name": "新模块", "parent": 10}
        )

    @patch.object(whart_tools.requests, "post")
    def test_add_module_failure(self, mock_post):
        class MockResponse:
            def raise_for_status(self):
                pass
            def json(self):
                return {
                    "code": 400,
                    "message": "创建失败"
                }
        mock_post.return_value = MockResponse()

        result = whart_tools.add_module(project_id=1, name="新模块")
        self.assertEqual(result["message"], "保存失败")
        self.assertEqual(result["response"]["code"], 400)


class WhartToolsFileManagementTests(unittest.TestCase):
    @patch.object(whart_tools.requests, "get", return_value=_JsonResponse({"count": 1, "results": [{"id": 8}]}))
    def test_list_files_passes_filters(self, mock_get):
        result = whart_tools.list_files(
            project_id=1,
            page=2,
            page_size=50,
            search="需求",
            status="available",
            extension=".docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ordering="original_name",
        )

        self.assertEqual(result["count"], 1)
        mock_get.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/files/",
            headers=whart_tools.HEADERS,
            params={
                "page": 2,
                "page_size": 50,
                "search": "需求",
                "status": "available",
                "extension": ".docx",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "ordering": "original_name",
            },
        )

    @patch.object(whart_tools.requests, "post", return_value=_JsonResponse({"file_id": 8, "name": "需求.txt"}, 201))
    def test_upload_file_uses_multipart_files_field(self, mock_post):
        with tempfile.TemporaryDirectory() as temp_root:
            file_path = os.path.join(temp_root, "需求.txt")
            with open(file_path, "wb") as f:
                f.write(b"# doc")

            result = whart_tools.upload_files(1, file_path)

        self.assertEqual(result["file_id"], 8)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"{whart_tools.BASE_URL}/api/projects/1/files/")
        self.assertEqual(kwargs["headers"], whart_tools.HEADERS)
        self.assertEqual(kwargs["files"][0][0], "files")
        self.assertEqual(kwargs["files"][0][1][0], "需求.txt")
        self.assertEqual(kwargs["files"][0][1][2], "text/plain")

    @patch.object(whart_tools.requests, "post", return_value=_JsonResponse({"valid": True, "files": []}))
    def test_validate_files_parses_csv_ids(self, mock_post):
        result = whart_tools.validate_files(1, "8, 9,8")

        self.assertEqual(result, {"valid": True, "files": []})
        mock_post.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/files/validate/",
            headers=whart_tools.HEADERS,
            json={"file_ids": [8, 9]},
        )

    @patch.object(
        whart_tools.requests,
        "post",
        return_value=_JsonResponse({"auto_delete_on_unbind": True, "auto_delete_zero_refs": False}),
    )
    def test_update_file_settings_parses_bool_values(self, mock_post):
        result = whart_tools.update_file_settings(1, "true", "false")

        self.assertTrue(result["auto_delete_on_unbind"])
        self.assertFalse(result["auto_delete_zero_refs"])
        mock_post.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/files/settings/",
            headers=whart_tools.HEADERS,
            json={"auto_delete_on_unbind": True, "auto_delete_zero_refs": False},
        )

    @patch.object(whart_tools.requests, "delete")
    def test_delete_file_handles_204(self, mock_delete):
        mock_delete.return_value = _JsonResponse({}, 204)

        result = whart_tools.delete_file(1, 8)

        self.assertTrue(result["success"])
        mock_delete.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/files/8/",
            headers=whart_tools.HEADERS,
        )

    @patch.object(whart_tools.requests, "get")
    def test_download_file_saves_response_content(self, mock_get):
        mock_get.return_value = _BinaryResponse(
            b"hello",
            {
                "Content-Type": "text/plain",
                "Content-Disposition": 'attachment; filename="hello.txt"',
            },
        )
        with tempfile.TemporaryDirectory() as temp_root:
            result = whart_tools.download_file(1, 8, output_dir=temp_root)
            saved_path = result["output_path"]
            with open(saved_path, "rb") as f:
                content = f.read()

        self.assertEqual(content, b"hello")
        self.assertTrue(saved_path.endswith("hello.txt"))
        mock_get.assert_called_once_with(
            f"{whart_tools.BASE_URL}/api/projects/1/files/8/download/",
            headers=whart_tools.HEADERS,
            stream=True,
        )


if __name__ == "__main__":
    unittest.main()
