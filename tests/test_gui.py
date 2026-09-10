from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import trimesh
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from ai_print_optimizer.gui import (
    JobWorker,
    build_preview_mesh,
    build_printed_preview_mesh,
    read_gcode_layer_heights,
    read_gcode_layer_preview,
)
from ai_print_optimizer.vanior_gui import (
    NAVIGATION_ASSET_DIR,
    NAVIGATION_ICON_FILES,
    SUPPORT_PROMPT_SETTINGS_KEY,
    SUPPORT_URL,
    MainWindow,
    MaterialProfileDialog,
    ModelCanvas,
    PrinterHardwareDialog,
    _nav_icon,
    _right_handed_view_coordinates,
    scan_model,
    suggest_output_path,
)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace_temp = tempfile.TemporaryDirectory()
        cls.previous_workspace = os.environ.get("VANIOR_PRINT_HOME")
        os.environ["VANIOR_PRINT_HOME"] = str(Path(cls.workspace_temp.name) / "VANIOR PRINT")
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.previous_workspace is None:
            os.environ.pop("VANIOR_PRINT_HOME", None)
        else:
            os.environ["VANIOR_PRINT_HOME"] = cls.previous_workspace
        cls.workspace_temp.cleanup()

    def test_sidebar_navigation_icons_are_rendered_for_every_page(self) -> None:
        kinds = (
            "overview",
            "analysis",
            "print",
            "preview",
            "optimization",
            "export",
            "history",
            "print_dna",
            "material",
            "printers",
            "settings",
        )
        for kind in kinds:
            icon = _nav_icon(kind)
            self.assertFalse(icon.isNull(), kind)
            pixmap = icon.pixmap(32, 32)
            self.assertFalse(pixmap.isNull(), kind)
            self.assertGreater(pixmap.toImage().depth(), 0, kind)

    def test_boosty_support_prompt_is_optional_and_shown_only_once(self) -> None:
        window = MainWindow()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                window.settings = QSettings(
                    str(Path(temporary) / "settings.ini"),
                    QSettings.Format.IniFormat,
                )
                support_button = object()
                continue_button = object()
                message_box = mock.Mock()
                message_box.addButton.side_effect = [support_button, continue_button]
                message_box.clickedButton.return_value = support_button
                with (
                    mock.patch(
                        "ai_print_optimizer.vanior_gui.QMessageBox",
                        return_value=message_box,
                    ) as message_box_class,
                    mock.patch(
                        "ai_print_optimizer.vanior_gui.QDesktopServices.openUrl",
                        return_value=True,
                    ) as open_url,
                ):
                    window._show_support_prompt_once()
                    window._show_support_prompt_once()

                message_box_class.assert_called_once_with(window)
                message_box.exec.assert_called_once_with()
                self.assertTrue(
                    window.settings.value(SUPPORT_PROMPT_SETTINGS_KEY, False, bool)
                )
                opened_url = open_url.call_args.args[0]
                self.assertEqual(opened_url.toString(), SUPPORT_URL)
        finally:
            window.close()

    def test_supplied_navigation_artwork_is_packaged_and_transparent(self) -> None:
        self.assertEqual(len(NAVIGATION_ICON_FILES), 11)
        for kind, filename in NAVIGATION_ICON_FILES.items():
            path = NAVIGATION_ASSET_DIR / filename
            self.assertTrue(path.is_file(), kind)
            image = _nav_icon(kind).pixmap(64, 64).toImage()
            self.assertTrue(image.hasAlphaChannel(), kind)
            self.assertEqual(image.pixelColor(0, 0).alpha(), 0, kind)

    def test_output_suggestion_never_selects_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "part.stl"
            source.touch()
            (Path(temporary) / "part-result").mkdir()
            suggested = suggest_output_path(source)
            self.assertEqual(suggested.name, "part-result-2")
            self.assertFalse(suggested.exists())

    def test_scan_stl_returns_serializable_geometry_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "box.stl"
            trimesh.creation.box(extents=(10, 20, 30)).export(source)
            events = []
            result = scan_model(
                source,
                None,
                material="PLA",
                plate=1,
                progress_callback=events.append,
                cancel_event=Event(),
            )
            self.assertEqual(result["kind"], "scan")
            self.assertEqual(result["report"]["health"]["status"], "READY")
            preview = result["preview_mesh"]
            self.assertGreater(len(preview["vertices"]), 0)
            self.assertGreater(len(preview["faces"]), 0)
            self.assertEqual(len(preview["vertices"][0]), 3)
            self.assertEqual(len(preview["faces"][0]), 3)
            self.assertEqual(events[-1].percent, 100)

    def test_solid_preview_accepts_worker_mesh_and_resets_view(self) -> None:
        canvas = ModelCanvas()
        canvas.set_mesh(
            {
                "vertices": [[-1, -1, 0], [1, -1, 0], [0, 1, 0]],
                "faces": [[0, 1, 2]],
            },
            "triangle.stl",
        )
        self.assertEqual(len(canvas._vertices), 3)
        self.assertEqual(len(canvas._faces), 1)
        canvas._zoom = 2.0
        canvas.reset_view()
        self.assertEqual(canvas._zoom, 1.0)

    def test_preview_builds_real_p1s_plate_and_preserves_ready_placement(self) -> None:
        canvas = ModelCanvas()
        canvas.set_mesh(
            {
                "vertices": [[-1, -1, -1], [1, -1, -1], [0, 1, 1]],
                "faces": [[0, 1, 2]],
                "source_center_mm": [64.0, 96.0, 10.0],
                "source_scale_mm": 20.0,
                "coordinate_source": "ready_3mf_build",
            },
            "placed.3mf",
        )
        left, right, back, front, plate_z = canvas._plate_bounds
        self.assertAlmostEqual(left, -3.2)
        self.assertAlmostEqual(right, 9.6)
        self.assertAlmostEqual(back, -4.8)
        self.assertAlmostEqual(front, 8.0)
        self.assertLess(plate_z, canvas._mesh_bounds[4])
        self.assertEqual(canvas._plate_surface_count, 6)
        self.assertGreater(canvas._plate_grid_count, 0)
        self.assertEqual(canvas._plate_border_count, 8)

    def test_preview_uses_detailed_and_interactive_levels_of_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "detailed.stl"
            trimesh.creation.icosphere(subdivisions=5, radius=10).export(source)
            mesh = build_preview_mesh(source, max_faces=12_000, interactive_faces=2_000)
            self.assertGreater(len(mesh["faces"]), len(mesh["interactive_faces"]))
            self.assertLessEqual(len(mesh["faces"]), 12_000)
            self.assertLessEqual(len(mesh["interactive_faces"]), 2_000)

            first = ModelCanvas(); second = ModelCanvas()
            first.set_mesh(mesh, "sphere")
            second.set_mesh(mesh, "sphere")
            self.assertIs(first._vertices, second._vertices)
            first.begin_interaction()
            self.assertTrue(first._interaction_active)
            previous_zoom = first._zoom
            first.zoom_view(1)
            self.assertGreater(first._zoom, previous_zoom)
            self.assertFalse(first._interaction_active)
            previous_zoom = first._zoom
            first.queue_zoom(1)
            self.assertGreater(first._zoom_target, previous_zoom)
            self.assertTrue(first._zoom_animation.isActive())
            first._animate_zoom()
            self.assertGreater(first._zoom, previous_zoom)
            first._zoom_animation.stop()
            first.begin_interaction()
            first.end_interaction()
            self.assertFalse(first._interaction_active)
            detail_blob, detail_count, interactive_blob, interactive_count = first._gpu_payload
            self.assertGreater(len(detail_blob), len(interactive_blob))
            self.assertEqual(detail_count, len(first._faces) * 3)
            self.assertEqual(interactive_count, len(first._interactive_faces) * 3)
            self.assertEqual(len(detail_blob), detail_count * 24)
            self.assertFalse(hasattr(first, "_zoom_snapshot"))
            self.assertIs(first._gpu_payload, second._gpu_payload)

    def test_printed_preview_prefers_final_3mf_build_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "ready.3mf"
            ready.touch()
            extracted_stl = Path(temporary) / "final.stl"
            trimesh.creation.box(extents=(40, 20, 10)).export(extracted_stl)
            extracted = mock.Mock(output_stl_path=extracted_stl)
            with mock.patch(
                "ai_print_optimizer.gui.extract_printable_stl",
                return_value=extracted,
            ) as extractor:
                preview = build_printed_preview_mesh(ready)
            extractor.assert_called_once()
            self.assertEqual(preview["coordinate_source"], "ready_3mf_build")
            self.assertEqual(preview["source_dimensions_mm"], [40.0, 20.0, 10.0])

    def test_preview_fallback_remains_bounded_when_decimation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "fallback.stl"
            trimesh.creation.icosphere(subdivisions=4, radius=10).export(source)
            with mock.patch.object(
                trimesh.Trimesh,
                "simplify_quadric_decimation",
                side_effect=RuntimeError("unsupported topology"),
            ):
                mesh = build_preview_mesh(source, max_faces=1_200, interactive_faces=600)
            self.assertGreater(len(mesh["faces"]), 0)
            self.assertLessEqual(len(mesh["faces"]), 1_200)
            self.assertLessEqual(len(mesh["interactive_faces"]), 600)

    def test_model_drag_direction_and_layer_clipping_are_connected(self) -> None:
        canvas = ModelCanvas()
        starting_yaw = canvas._yaw
        starting_pitch = canvas._pitch
        canvas.rotate_view(-20, 0)
        self.assertLess(canvas._yaw, starting_yaw)
        canvas.rotate_view(0, -20)
        self.assertLess(canvas._pitch, starting_pitch)
        clipped = canvas._clip_polygon_to_z(
            [(-1.0, 0.0, -1.0), (1.0, 0.0, -1.0), (0.0, 0.0, 1.0)],
            0.0,
        )
        self.assertEqual(len(clipped), 4)
        self.assertTrue(all(vertex[2] <= 0.0 for vertex in clipped))
        canvas.set_layer(5, 10, fraction=0.42, z_mm=4.2)
        self.assertEqual(canvas._layer_index, 5)
        self.assertAlmostEqual(canvas._layer_fraction, 0.42)

    def test_layer_cutoff_uses_exact_physical_z_coordinate(self) -> None:
        canvas = ModelCanvas()
        canvas.set_mesh(
            {
                "vertices": [[-1, -1, -1], [1, -1, -1], [0, 1, 1]],
                "faces": [[0, 1, 2]],
                "source_center_mm": [100.0, 100.0, 10.0],
                "source_scale_mm": 10.0,
            },
            "exact-z.stl",
        )
        canvas.set_layer(2, 4, fraction=0.5, z_mm=5.0)
        self.assertAlmostEqual(canvas._selected_layer_scene_z(), -0.5)
        canvas.set_layer(4, 4, fraction=1.0, z_mm=9.8)
        self.assertAlmostEqual(canvas._selected_layer_scene_z(), 1.0)

    def test_preview_camera_basis_is_right_handed_and_not_mirrored(self) -> None:
        x_axis = _right_handed_view_coordinates((1.0, 0.0, 0.0), 0.0, 0.0)
        y_axis = _right_handed_view_coordinates((0.0, 1.0, 0.0), 0.0, 0.0)
        z_axis = _right_handed_view_coordinates((0.0, 0.0, 1.0), 0.0, 0.0)
        matrix = np.asarray((x_axis, y_axis, z_axis), dtype=np.float64).T
        self.assertGreater(float(np.linalg.det(matrix)), 0.0)

    def test_bambu_gcode_layer_heights_are_read_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gcode = Path(temporary) / "plate_1.gcode"
            gcode.write_text(
                "; total layer number: 3\n"
                "G90\nM83\n"
                "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\nG1 X10 Y10\nG1 X20 Y10 E0.5\n"
                "; CHANGE_LAYER\n; Z_HEIGHT: 0.32\nG1 X20 Y20 E0.4\n"
                "; CHANGE_LAYER\n; Z_HEIGHT: 0.44\nG1 X10 Y20 E0.3\n",
                encoding="utf-8",
            )
            self.assertEqual(read_gcode_layer_heights(gcode), [0.2, 0.32, 0.44])
            preview = read_gcode_layer_preview(gcode)
            self.assertEqual(preview["z_mm"], [0.2, 0.32, 0.44])
            self.assertEqual([len(layer) for layer in preview["paths"]], [1, 1, 1])

    def test_layer_preview_preserves_model_support_and_interface_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gcode = Path(temporary) / "plate_1.gcode"
            gcode.write_text(
                "G90\nM83\n"
                "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n"
                "; FEATURE: Outer wall\nG1 X0 Y0\nG1 X10 Y0 E0.5\n"
                "; FEATURE: Support\nG1 X10 Y10 E0.4\n"
                "; FEATURE: Support interface\nG1 X0 Y10 E0.3\n",
                encoding="utf-8",
            )
            preview = read_gcode_layer_preview(gcode)
            self.assertEqual(
                [segment[4] for segment in preview["paths"][0]],
                ["outer_wall", "support", "support_interface"],
            )
            self.assertEqual(preview["role_counts"]["support"], 1)
            self.assertEqual(preview["role_counts"]["support_interface"], 1)

    def test_layer_preview_uses_final_mesh_coordinate_frame(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gcode = Path(temporary) / "plate_1.gcode"
            gcode.write_text(
                "G90\nM83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n"
                "; FEATURE: Outer wall\nG1 X90 Y200\nG1 X110 Y200 E0.5\n",
                encoding="utf-8",
            )
            preview = read_gcode_layer_preview(
                gcode,
                coordinate_mesh={
                    "source_center_mm": [100.0, 200.0, 5.0],
                    "source_scale_mm": 10.0,
                },
            )
            self.assertEqual(preview["paths"][0][0][:4], [-1.0, 0.0, 1.0, 0.0])

    def test_layer_preview_distinguishes_all_slicer_path_families(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gcode = Path(temporary) / "plate_1.gcode"
            features = (
                "Inner wall", "Top surface", "Bottom surface",
                "Sparse infill", "Internal solid infill", "Bridge infill", "Brim",
            )
            lines = ["G90", "M83", "; CHANGE_LAYER", "; Z_HEIGHT: 0.2", "G1 X0 Y0"]
            for index, feature in enumerate(features, start=1):
                lines.extend((f"; FEATURE: {feature}", f"G1 X{index} Y{index} E0.2"))
            gcode.write_text("\n".join(lines), encoding="utf-8")
            preview = read_gcode_layer_preview(gcode)
            self.assertEqual(
                [segment[4] for segment in preview["paths"][0]],
                [
                    "inner_wall", "top_surface", "bottom_surface", "infill",
                    "solid_infill", "bridge", "skirt_brim",
                ],
            )

    def test_supports_keep_their_position_outside_the_model_footprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gcode = Path(temporary) / "plate_1.gcode"
            gcode.write_text(
                "G90\nM83\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n"
                "; FEATURE: Outer wall\nG1 X0 Y0\nG1 X10 Y0 E0.2\n"
                "; FEATURE: Support\nG1 X20 Y0 E0.2\n",
                encoding="utf-8",
            )
            preview = read_gcode_layer_preview(gcode)
            model_path, support_path = preview["paths"][0]
            self.assertLessEqual(max(model_path[:4]), 1.0)
            self.assertGreater(support_path[2], 1.0)

    def test_gpu_shader_keeps_unlit_toolpaths_out_of_mesh_clipping(self) -> None:
        shader = ModelCanvas._FRAGMENT_SHADER
        self.assertIn("unlitMode < 0.5 || unlitMode > 1.5", shader)

    def test_support_preview_builds_cumulative_printable_width_geometry(self) -> None:
        canvas = ModelCanvas()
        canvas.set_mesh(
            {
                "vertices": [[-1, -1, -1], [1, -1, -1], [0, 1, 1]],
                "faces": [[0, 1, 2]],
                "source_center_mm": [0.0, 0.0, 5.0],
                "source_scale_mm": 5.0,
            },
            "supports.stl",
        )
        canvas.set_layer_paths(
            [
                [[-0.5, 0.0, 0.5, 0.0, "support"]],
                [[-0.4, 0.0, 0.4, 0.0, "support_interface"]],
            ],
            [0.2, 0.4],
        )
        self.assertEqual(
            [role for role, _, _ in canvas._support_ranges],
            ["support", "support_interface"],
        )
        self.assertEqual(canvas._support_vertex_count, 12)
        self.assertEqual(len(canvas._support_blob), 12 * 24)
        canvas._support_gpu_dirty = False
        canvas.set_layer(1, 2, z_mm=0.2)
        self.assertFalse(
            canvas._support_gpu_dirty,
            "moving the layer slider must not re-upload the full support volume",
        )

    def test_multiple_priority_cards_create_one_canonical_blended_value(self) -> None:
        window = MainWindow()
        try:
            window.priority_radios["fast"].setChecked(True)
            window.priority_radios["quality"].setChecked(True)
            window.priority_radios["balanced"].setChecked(False)
            self.assertEqual(window.priority_combo.currentData(), "quality+fast")
            self.assertIn("Среднее между", window.priority_mix_label.text())
        finally:
            window.close()

    def test_print_dna_overall_quality_is_a_described_ten_point_menu(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual(window.dna_quality.count(), 10)
            self.assertEqual(
                [window.dna_quality.itemData(index) for index in range(10)],
                list(range(1, 11)),
            )
            self.assertEqual(window.dna_quality.currentData(), 8)
            self.assertIn("превосходно", window.dna_quality.itemText(9))
        finally:
            window.close()

    def test_result_slider_reveals_exact_selected_layer(self) -> None:
        window = MainWindow()
        try:
            window.preview_canvas.set_mesh(
                {
                    "vertices": [[-1, -1, -1], [1, -1, -1], [0, 1, 1]],
                    "faces": [[0, 1, 2]],
                },
                "layered.stl",
            )
            window._render_result(
                {
                    "dimensions": [10, 10, 6],
                    "layer_z_mm": [0.2, 0.4, 0.6],
                    "layer_paths": [[], [[-1, 0, 1, 0]], []],
                }
            )
            self.assertEqual(window.layer_slider.maximum(), 3)
            window.layer_slider.setValue(2)
            self.assertAlmostEqual(window.preview_canvas._layer_fraction, 2 / 3)
            self.assertEqual(window.preview_canvas._layer_index, 2)
            self.assertEqual(len(window.preview_canvas._layer_paths[1]), 1)
            self.assertIn("Z 0.40 мм", window.layer_label.text())
        finally:
            window.close()

    def test_window_starts_in_optimize_mode(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual(window.mode_combo.currentData(), "optimize")
            self.assertIn("STL", window.model_edit.placeholderText())
            self.assertTrue(window.model_edit.isHidden())
            self.assertNotIn(
                "Текущая модель",
                [label.text() for label in window.findChildren(QLabel)],
            )
            self.assertFalse(hasattr(window, "profile_edit"))
            self.assertEqual(window.printer_combo.currentData(), "Bambu Lab P1S")
            self.assertEqual(
                [window.nozzle_combo.itemData(index) for index in range(window.nozzle_combo.count())],
                [0.2, 0.4, 0.6, 0.8],
            )
            self.assertEqual(
                {window.material_combo.itemData(index) for index in range(window.material_combo.count())},
                {"PLA", "PETG"},
            )
            self.assertIn("Bambu Lab PLA Basic", [window.material_combo.itemText(index) for index in range(window.material_combo.count())])
            self.assertIn("eSUN PETG", [window.material_combo.itemText(index) for index in range(window.material_combo.count())])
            self.assertEqual(window.sidebar_material.cursor().shape(), Qt.CursorShape.PointingHandCursor)
            self.assertLessEqual(window._nav_buttons["overview"].parent().width(), 82)
            for key, _, label in window.PAGE_NAMES:
                button = window._nav_buttons[key]
                self.assertEqual(button.text(), "")
                self.assertEqual(button.toolTip(), label)
                self.assertEqual(button.accessibleName(), label)
                self.assertEqual(button.iconSize().width(), 36)
                self.assertEqual(button.iconSize().height(), 36)
            self.assertIn("Выбрать пластик", window.sidebar_material.toolTip())
            self.assertIn("Выбрать сопло", window.sidebar_printer.toolTip())
            self.assertIn("Bambu Lab P1S", window.sidebar_printer.toolTip())
            self.assertTrue(
                set(str(window.priority_combo.currentData()).split("+"))
                <= {"quality", "strength", "balanced", "fast"}
            )
            self.assertGreaterEqual(window.priority_combo.count(), 3)
            self.assertEqual(window.purpose_combo.count(), 3)
            self.assertIn("print_dna", window._page_indexes)
            self.assertTrue(hasattr(window, "dna_profile_summary"))
            self.assertTrue(window.quality_search.isChecked())
            self.assertFalse(window.cancel_button.isEnabled())
            self.assertTrue(window.scroll_area.widgetResizable())
            self.assertGreaterEqual(window.model_edit.minimumHeight(), 24)
            self.assertGreaterEqual(window.result_stack.minimumHeight(), 190)
        finally:
            window.close()

    def test_sidebar_quick_actions_use_separate_material_and_hardware_dialogs(self) -> None:
        material = MaterialProfileDialog("PETG")
        hardware = PrinterHardwareDialog("Bambu Lab P1S", 0.6, "Engineering Plate")
        try:
            self.assertEqual(material.selected_material, "Generic PETG")
            self.assertEqual(material.selected_family, "PETG")
            self.assertFalse(hasattr(material, "nozzle"))
            self.assertEqual(hardware.printer_name.text(), "Bambu Lab P1S")
            self.assertAlmostEqual(hardware.selected_nozzle_mm, 0.6)
            self.assertEqual(hardware.selected_bed_type, "Engineering Plate")
            self.assertEqual(hardware.build_plate.count(), 4)
            self.assertFalse(hasattr(hardware, "material"))
        finally:
            material.close()
            hardware.close()

    def test_sidebar_page_transition_is_directional_and_interruptible(self) -> None:
        window = MainWindow()
        try:
            window._show_page("analysis")
            first_transition = window._page_transition
            first_widget = window.pages.currentWidget()
            self.assertIsNotNone(first_transition)
            self.assertIsNotNone(window._page_transition_effect)
            self.assertEqual(first_transition.duration(), window.PAGE_TRANSITION_MS)
            self.assertEqual(window.pages.currentIndex(), window._page_indexes["analysis"])
            self.assertTrue(window._nav_buttons["analysis"].isChecked())
            self.assertEqual(
                first_widget.pos().x(),
                window._page_transition_origin.x() + window.PAGE_TRANSITION_OFFSET,
            )

            window._show_page("print")
            self.assertIsNot(window._page_transition, first_transition)
            self.assertEqual(window.pages.currentIndex(), window._page_indexes["print"])
            self.assertTrue(window._nav_buttons["print"].isChecked())

            active_widget = window._page_transition_widget
            origin = window._page_transition_origin
            window._finish_page_transition()
            self.assertIsNone(window._page_transition)
            self.assertIsNone(active_widget.graphicsEffect())
            self.assertEqual(active_widget.pos(), origin)
        finally:
            window._finish_page_transition()
            window.close()

    def test_sidebar_label_is_shown_immediately_on_hover(self) -> None:
        window = MainWindow()
        try:
            button = window._nav_buttons["analysis"]
            with mock.patch("ai_print_optimizer.vanior_gui.QToolTip.showText") as show_text:
                button._show_tooltip()
            show_text.assert_called_once()
            self.assertEqual(show_text.call_args.args[1], "Анализ модели")
            self.assertIs(show_text.call_args.args[2], button)
        finally:
            window.close()

    def test_selecting_model_never_loads_mesh_on_event_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "box.stl"
            trimesh.creation.box(extents=(10, 20, 30)).export(source)
            window = MainWindow()
            try:
                window._start_scan = mock.Mock()
                with mock.patch.object(
                    ModelCanvas,
                    "set_model",
                    side_effect=AssertionError("synchronous mesh load"),
                ):
                    window._model_selected(str(source))
                window._start_scan.assert_called_once_with()
                self.assertFalse(source.exists())
                self.assertEqual(window._current_source().parent, window.workspace.uploads)
            finally:
                window.close()

    def test_completed_result_does_not_compress_form_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "result.3mf"
            ready.write_bytes(b"verified-project")
            window = MainWindow()
            try:
                window.resize(975, 918)
                window.show()
                window._on_completed(
                    {
                        "kind": "pipeline",
                        "strategy": "none",
                        "print_time": 1_912,
                        "mass": 4.5,
                        "profile_setting_count": 438,
                        "printer": "Bambu Lab P1S",
                        "print_priority": "balanced",
                        "model_purpose": "decorative",
                        "reason": "Готово без поддержек.",
                        "ready_3mf": str(ready),
                        "ready_gcode": "",
                        "output": temporary,
                    }
                )
                self.app.processEvents()

                self.assertGreaterEqual(window.model_edit.height(), 30)
                self.assertGreaterEqual(window.mode_combo.height(), 30)
                self.assertGreaterEqual(window.output_edit.height(), 30)
                self.assertGreaterEqual(window.result_stack.height(), 190)
                self.assertGreaterEqual(window.scroll_area.verticalScrollBar().maximum(), 0)
                self.assertEqual(Path(window.last_result["ready_3mf"]).parent, window.workspace.ready)
            finally:
                window.close()

    def test_manual_controls_are_forwarded_to_the_print_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "part.stl"
            trimesh.creation.box().export(source)
            window = MainWindow()
            try:
                window.model_edit.setText(str(source))
                window.manual_settings_enabled = True
                window.first_layer_height.setValue(0.24)
                window.line_width.setValue(0.44)
                window.ironing.setChecked(True)
                window.support_enable.setChecked(True)
                window.support_type.setCurrentIndex(2)

                options = window._job_options("optimize")

                self.assertIsNotNone(options)
                assert options is not None
                self.assertEqual(options["support_strategy"], "tree")
                self.assertEqual(options["print_setting_overrides"]["initial_layer_height_mm"], 0.24)
                self.assertEqual(options["print_setting_overrides"]["line_width_mm"], 0.44)
                self.assertTrue(options["print_setting_overrides"]["ironing_enabled"])
                self.assertIn("print_dna_profile", options)
                self.assertTrue(options["quality_search"])
                self.assertEqual(Path(options["output"]).parent, window.workspace.ready / "Проекты")
            finally:
                window.close()

    def test_independent_result_enables_main_open_file_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ready = Path(temporary) / "result.gcode"
            ready.write_text("; generated by VANIOR Slice\n", encoding="utf-8")
            window = MainWindow()
            try:
                window.last_result = {
                    "kind": "vanior-slice",
                    "ready_3mf": "",
                    "ready_gcode": str(ready),
                    "output": temporary,
                }
                window._render_result(window.last_result)
                window._update_ui_state()

                self.assertTrue(window.preview_header_button.isEnabled())
                self.assertTrue(window.open_3mf_button.isEnabled())
                self.assertIn("G-code", window.preview_header_button.text())
                self.assertIn("G-code", window.open_3mf_button.text())
                self.assertEqual(window.result_title.text(), "Инженерный G-code создан")
                with mock.patch("ai_print_optimizer.vanior_gui.subprocess.Popen") as popen:
                    window._open_ready_result()
                popen.assert_called_once_with(
                    ["explorer.exe", "/select,", str(ready)]
                )
            finally:
                window.close()

    def test_independent_3mf_is_verified_before_opening(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "result.gcode.3mf"
            package.write_bytes(b"PK test package")
            window = MainWindow()
            try:
                window.last_result = {
                    "kind": "vanior-slice",
                    "ready_3mf": str(package),
                    "ready_gcode": "",
                    "output": temporary,
                }
                verification = mock.Mock(valid=True, errors=())
                with (
                    mock.patch(
                        "ai_print_optimizer.vanior_gui.verify_vanior_gcode_3mf",
                        return_value=verification,
                    ) as verify,
                    mock.patch("ai_print_optimizer.vanior_gui.os.startfile") as startfile,
                ):
                    window._open_ready_3mf()

                verify.assert_called_once_with(package)
                startfile.assert_called_once_with(package)
            finally:
                window.close()

    def test_only_vanior_engine_is_used_for_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "independent.stl"
            trimesh.creation.box(extents=(10, 10, 4)).export(source)
            window = MainWindow()
            try:
                window.model_edit.setText(str(source))
                index = window.engine_combo.findData("vanior")
                self.assertGreaterEqual(index, 0)
                window.engine_combo.setCurrentIndex(index)

                options = window._job_options("vanior")

                self.assertIsNotNone(options)
                assert options is not None
                self.assertEqual(options["slicer_backend"], "vanior")
                self.assertEqual(options["slicer"], "")
                self.assertTrue(window.export_3mf.isChecked())
                self.assertTrue(window.export_3mf.isEnabled())
            finally:
                window.close()

    def test_independent_gui_worker_uses_event_for_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "worker-box.stl"
            trimesh.creation.box(extents=(12.0, 12.0, 3.0)).export(source)
            completed = []
            failures = []
            progress = []
            worker = JobWorker(
                "vanior",
                {
                    "source": str(source),
                    "output": str(root / "output"),
                    "material": "PLA",
                    "plate": 1,
                    "nozzle": 0.4,
                    "printer": "Bambu Lab P1S",
                    "print_priority": "balanced",
                    "model_purpose": "decorative",
                    "print_setting_overrides": {},
                    "overhang_angle_deg": 45.0,
                },
                Event(),
            )
            worker.completed.connect(completed.append)
            worker.failed.connect(failures.append)
            worker.progress.connect(lambda *event: progress.append(event))

            worker.run()

            self.assertFalse(failures)
            self.assertEqual(len(completed), 1)
            self.assertTrue(progress)
            self.assertTrue(Path(completed[0]["ready_gcode"]).is_file())
            self.assertTrue(Path(completed[0]["ready_3mf"]).is_file())
            self.assertTrue(
                str(completed[0]["ready_3mf"]).casefold().endswith(".gcode.3mf")
            )
            self.assertEqual(
                {item["strategy"] for item in completed[0]["support_comparison"]},
                {"none", "normal", "tree"},
            )
            self.assertEqual(completed[0]["strategy"], "none")
            self.assertIsNotNone(completed[0]["orientation"])

    def test_every_distribution_exposes_only_vanior_engine(self) -> None:
        window = MainWindow()
        try:
            self.assertEqual(window.engine_combo.count(), 1)
            self.assertEqual(window.engine_combo.currentData(), "vanior")
            self.assertFalse(window.engine_combo.isEnabled())
            self.assertIn("не ищет, не запускает", window.engine_note.text())
            self.assertEqual(window.detect_engine_button.text(), "Проверить VANIOR Slice")
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
