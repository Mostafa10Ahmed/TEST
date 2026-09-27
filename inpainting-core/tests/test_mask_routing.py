import ast
import unittest
from pathlib import Path

import cv2
import numpy as np


def _load_routing_helpers():
    source = Path(__file__).parents[1].joinpath("app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "_safe_bubble_interior",
        "_component_background_is_uniform_light",
        "_find_system_panel_interior",
        "_extract_precise_text_mask",
        "_route_text_masks",
        "_blend_complex_result",
        "_has_new_boundary_artifact",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "cv2": cv2,
        "np": np,
        "SAFE_BUBBLE_BORDER": 5,
        "UNIFORM_LIGHT_STD": 12.0,
        "UNIFORM_LIGHT_GRADIENT": 8.0,
        "COMPLEX_FEATHER_RADIUS": 2.5,
        "MIN_COMP_AREA": 20,
        "C_CONSTANT": 13,
    }
    exec(compile(module, "routing_helpers", "exec"), namespace)
    return namespace


HELPERS = _load_routing_helpers()


class MaskRoutingTests(unittest.TestCase):
    def setUp(self):
        self.interior = np.full((80, 80), 255, dtype=np.uint8)
        self.mask = np.zeros((80, 80), dtype=np.uint8)
        cv2.rectangle(self.mask, (32, 32), (47, 47), 255, thickness=-1)

    def test_uniform_light_bubble_keeps_legacy_path(self):
        image = np.full((80, 80, 3), 250, dtype=np.uint8)
        legacy, complex_mask, excluded = HELPERS["_route_text_masks"](image, self.mask, self.interior)
        self.assertGreater(cv2.countNonZero(legacy), 0)
        self.assertEqual(cv2.countNonZero(complex_mask), 0)
        self.assertEqual(cv2.countNonZero(excluded), 0)

    def test_gradient_bubble_never_uses_white_path(self):
        gradient = np.tile(np.linspace(190, 250, 80, dtype=np.uint8), (80, 1))
        image = np.dstack([gradient, gradient, gradient])
        legacy, complex_mask, excluded = HELPERS["_route_text_masks"](image, self.mask, self.interior)
        self.assertEqual(cv2.countNonZero(legacy), 0)
        self.assertGreater(cv2.countNonZero(complex_mask), 0)
        self.assertEqual(cv2.countNonZero(excluded), 0)

    def test_complex_text_near_outline_is_clipped(self):
        image = np.tile(np.linspace(180, 240, 80, dtype=np.uint8), (80, 1))
        image = np.dstack([image, image, image])
        edge_mask = np.zeros((80, 80), dtype=np.uint8)
        cv2.rectangle(edge_mask, (1, 30), (16, 46), 255, thickness=-1)
        legacy, complex_mask, excluded = HELPERS["_route_text_masks"](image, edge_mask, self.interior)
        self.assertEqual(cv2.countNonZero(legacy), 0)
        self.assertGreater(cv2.countNonZero(excluded), 0)
        self.assertEqual(cv2.countNonZero(complex_mask[:, :5]), 0)

    def test_complex_blend_never_changes_pixels_outside_mask(self):
        source = np.full((40, 40, 3), 100, dtype=np.uint8)
        restored = np.full((40, 40, 3), 200, dtype=np.uint8)
        mask = np.zeros((40, 40), dtype=np.uint8)
        cv2.circle(mask, (20, 20), 8, 255, thickness=-1)
        blended = HELPERS["_blend_complex_result"](source, restored, mask)
        self.assertTrue(np.array_equal(blended[mask == 0], source[mask == 0]))
        self.assertGreater(int(blended[mask > 0].mean()), 100)

    def test_new_hard_edge_is_rejected_on_smooth_background(self):
        source = np.full((40, 40, 3), 200, dtype=np.uint8)
        candidate = source.copy()
        mask = np.zeros((40, 40), dtype=np.uint8)
        cv2.circle(mask, (20, 20), 8, 255, thickness=-1)
        candidate[mask > 0] = 20
        self.assertTrue(HELPERS["_has_new_boundary_artifact"](source, candidate, mask))

    def test_bordered_system_panel_exposes_only_its_interior(self):
        image = np.full((100, 140, 3), 180, dtype=np.uint8)
        cv2.rectangle(image, (20, 20), (120, 80), (25, 25, 25), thickness=-1)
        cv2.rectangle(image, (20, 20), (120, 80), (220, 220, 220), thickness=2)
        text = np.zeros((100, 140), dtype=np.uint8)
        cv2.rectangle(text, (48, 43), (92, 55), 255, thickness=-1)
        panel = HELPERS["_find_system_panel_interior"](image, text)
        self.assertEqual(int(panel[50, 70]), 255)
        self.assertEqual(int(panel[20, 20]), 0)

    def test_precise_mask_does_not_fill_a_text_detector_box(self):
        image = np.full((100, 160, 3), 245, dtype=np.uint8)
        cv2.putText(image, "TEST", (38, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (15, 15, 15), 2)
        detector_box = np.zeros((100, 160), dtype=np.uint8)
        cv2.rectangle(detector_box, (25, 30), (135, 75), 255, thickness=-1)
        precise = HELPERS["_extract_precise_text_mask"](image, detector_box, 13)
        density = cv2.countNonZero(precise) / cv2.countNonZero(detector_box)
        self.assertGreater(density, 0.01)
        self.assertLess(density, 0.65)


if __name__ == "__main__":
    unittest.main()
