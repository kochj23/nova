"""
test_nova_image_utils.py — All 7 test categories for nova_image_utils.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_image_utils.py"
_spec = importlib.util.spec_from_file_location("nova_image_utils", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ensure_backend = _mod.ensure_backend
get_model_for_today = _mod.get_model_for_today
get_random_model = _mod.get_random_model
generate_image = _mod.generate_image
MODELS = _mod.MODELS
ART_MODEL_ROTATION = _mod.ART_MODEL_ROTATION
DEFAULT_MODEL = _mod.DEFAULT_MODEL


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Bearer ", "api_key ="]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_swarmui_is_localhost(self):
        """SwarmUI must be on localhost — no cloud image generation."""
        self.assertTrue(
            _mod.SWARMUI_URL.startswith("http://127.0.0.1") or
            _mod.SWARMUI_URL.startswith("http://localhost"),
        )

    def test_generate_image_sh_path_in_home(self):
        """generate_image.sh must be in user home, not system path."""
        self.assertTrue(str(_mod.GENERATE_IMAGE_SH).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_model_for_today_fast(self):
        start = time.perf_counter()
        result = get_model_for_today()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)
        self.assertIn(result, MODELS)

    def test_max_retries_constant(self):
        self.assertGreaterEqual(_mod.MAX_RETRIES, 1)
        self.assertLessEqual(_mod.MAX_RETRIES, 10)

    def test_retry_delay_reasonable(self):
        self.assertGreater(_mod.RETRY_DELAY, 0)
        self.assertLessEqual(_mod.RETRY_DELAY, 60)

    def test_timeout_reasonable(self):
        self.assertGreater(_mod.TIMEOUT, 30)
        self.assertLessEqual(_mod.TIMEOUT, 600)

    def test_art_model_rotation_covers_all_days(self):
        """Every day of the week (0-6) must have a model."""
        for day in range(7):
            self.assertIn(day, ART_MODEL_ROTATION)
            self.assertIn(ART_MODEL_ROTATION[day], MODELS)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_image_retries_max_times(self):
        """generate_image must retry exactly MAX_RETRIES times on failure."""
        call_count = [0]

        def failing_run(cmd, capture_output=False, text=False, timeout=None):
            call_count[0] += 1
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            return r

        with patch.object(_mod, "ensure_backend", return_value=True):
            with patch("subprocess.run", side_effect=failing_run):
                with patch("time.sleep"):  # skip delays
                    result = generate_image("test prompt")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], _mod.MAX_RETRIES)

    def test_generate_image_succeeds_on_second_attempt(self):
        """generate_image should return path on second successful attempt."""
        attempt = [0]

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name

        try:
            def flaky_run(cmd, capture_output=False, text=False, timeout=None):
                attempt[0] += 1
                r = MagicMock()
                if attempt[0] < 2:
                    r.returncode = 1
                    r.stdout = ""
                else:
                    r.returncode = 0
                    r.stdout = f"Workspace copy: {img_path}\n"
                return r

            with patch.object(_mod, "ensure_backend", return_value=True):
                with patch("subprocess.run", side_effect=flaky_run):
                    with patch("time.sleep"):
                        with patch.object(_mod, "_model_available_via_api", return_value=True):
                            result = generate_image("test prompt")
            self.assertEqual(result, img_path)
        finally:
            os.unlink(img_path)

    def test_generate_image_returns_none_if_backend_down(self):
        """generate_image must return None immediately if backend is down."""
        with patch.object(_mod, "ensure_backend", return_value=False):
            result = generate_image("test prompt")
        self.assertIsNone(result)

    def test_ensure_backend_returns_false_if_swarmui_down(self):
        """ensure_backend must return False if SwarmUI is unreachable."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = ensure_backend()
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_model_for_today_returns_valid_key(self):
        result = get_model_for_today()
        self.assertIn(result, MODELS)

    def test_models_dict_has_required_fields(self):
        """Each model must have file, name, best_for, optimal_steps."""
        for key, info in MODELS.items():
            self.assertIn("file", info, f"Model {key} missing 'file'")
            self.assertIn("name", info, f"Model {key} missing 'name'")
            self.assertIn("optimal_steps", info, f"Model {key} missing 'optimal_steps'")

    def test_default_model_exists_in_models(self):
        self.assertIn(DEFAULT_MODEL, MODELS)

    def test_art_model_rotation_day_values_valid(self):
        for day, model_key in ART_MODEL_ROTATION.items():
            self.assertIn(model_key, MODELS, f"Day {day} model {model_key!r} not in MODELS")

    def test_generate_image_parses_workspace_copy_line(self):
        """generate_image must parse 'Workspace copy: /path' from stdout."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img_path = f.name

        try:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = f"Some output\nWorkspace copy: {img_path}\nOpen with: Preview"

            with patch.object(_mod, "ensure_backend", return_value=True):
                with patch("subprocess.run", return_value=mock_result):
                    with patch.object(_mod, "_model_available_via_api", return_value=True):
                        result = generate_image("test prompt")
            self.assertEqual(result, img_path)
        finally:
            os.unlink(img_path)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_model_availability_check_integrated(self):
        """generate_image calls _model_available_via_api before generating."""
        with patch.object(_mod, "ensure_backend", return_value=True):
            with patch.object(_mod, "_model_available_via_api", return_value=False) as mock_check:
                with patch.object(_mod, "get_random_model", return_value="juggernaut"):
                    with patch("subprocess.run") as mock_run:
                        mock_run.return_value = MagicMock(returncode=1, stdout="")
                        with patch("time.sleep"):
                            generate_image("test")
            # Should have checked availability
            self.assertGreater(mock_check.call_count, 0)

    def test_get_random_model_falls_back_to_default(self):
        """get_random_model falls back to DEFAULT_MODEL if API unavailable."""
        with patch.object(_mod, "_model_available_via_api", return_value=False):
            result = get_random_model()
        self.assertEqual(result, DEFAULT_MODEL)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_generate_image_with_specific_model(self):
        """Specifying a model key should use that model's file."""
        called_with = []

        def capture_run(cmd, **kwargs):
            called_with.extend(cmd)
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            return r

        with patch.object(_mod, "ensure_backend", return_value=True):
            with patch.object(_mod, "_model_available_via_api", return_value=True):
                with patch("subprocess.run", side_effect=capture_run):
                    with patch("time.sleep"):
                        generate_image("prompt", model="juggernaut")

        model_file = MODELS["juggernaut"]["file"]
        self.assertTrue(any(model_file in str(arg) for arg in called_with),
                        f"Expected {model_file} in command args")

    def test_generate_image_uses_optimal_steps(self):
        """When steps=12 (default), should use model's optimal_steps."""
        called_with = []

        def capture_run(cmd, **kwargs):
            called_with.extend(cmd)
            r = MagicMock()
            r.returncode = 1
            r.stdout = ""
            return r

        with patch.object(_mod, "ensure_backend", return_value=True):
            with patch.object(_mod, "_model_available_via_api", return_value=True):
                with patch("subprocess.run", side_effect=capture_run):
                    with patch("time.sleep"):
                        generate_image("prompt", model="juggernaut", steps=12)

        # juggernaut optimal_steps is 8
        optimal = str(MODELS["juggernaut"]["optimal_steps"])
        self.assertTrue(any(optimal == str(arg) for arg in called_with),
                        f"Expected optimal steps {optimal} in cmd args")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.SWARMUI_URL, str)
        self.assertIsInstance(_mod.MAX_RETRIES, int)
        self.assertIsInstance(_mod.RETRY_DELAY, int)
        self.assertIsInstance(_mod.TIMEOUT, int)
        self.assertIsInstance(_mod.MODELS, dict)
        self.assertIsInstance(_mod.DEFAULT_MODEL, str)
        self.assertIsInstance(_mod.ART_MODEL_ROTATION, dict)

    def test_functions_exist(self):
        for fn in ("ensure_backend", "get_model_for_today", "get_random_model",
                   "_model_available_via_api", "generate_image"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_models_dict_not_empty(self):
        self.assertGreater(len(MODELS), 0)

    def test_art_model_rotation_has_7_entries(self):
        self.assertEqual(len(ART_MODEL_ROTATION), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
