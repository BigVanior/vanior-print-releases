from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import trimesh

from ai_print_optimizer.geometry_features import LocalModifierPlan, LocalModifierRange
from ai_print_optimizer.project3mf import (
    CORE_NS,
    PRODUCTION_NS,
    ObjectPrintConfiguration,
    create_bambu_project_from_3mf_objects,
    create_bambu_project_from_stl,
    create_bambu_project_from_stl_objects,
    extract_printable_stl,
    extract_stl_objects,
    verify_ready_project,
)
from ai_print_optimizer.report import PrintSettings


def _mesh_part(mesh: trimesh.Trimesh, object_id: str = "1") -> bytes:
    model = ET.Element(f"{{{CORE_NS}}}model", {"unit": "millimeter"})
    resources = ET.SubElement(model, f"{{{CORE_NS}}}resources")
    obj = ET.SubElement(
        resources, f"{{{CORE_NS}}}object", {"id": object_id, "type": "model"}
    )
    mesh_node = ET.SubElement(obj, f"{{{CORE_NS}}}mesh")
    vertices = ET.SubElement(mesh_node, f"{{{CORE_NS}}}vertices")
    for x, y, z in mesh.vertices:
        ET.SubElement(
            vertices,
            f"{{{CORE_NS}}}vertex",
            {"x": str(x), "y": str(y), "z": str(z)},
        )
    triangles = ET.SubElement(mesh_node, f"{{{CORE_NS}}}triangles")
    for v1, v2, v3 in mesh.faces:
        ET.SubElement(
            triangles,
            f"{{{CORE_NS}}}triangle",
            {"v1": str(v1), "v2": str(v2), "v3": str(v3)},
        )
    return ET.tostring(model, encoding="utf-8", xml_declaration=True)


def _write_bambu_project(path: Path, *, second_printable: bool = False) -> None:
    first = trimesh.creation.box(extents=(2.0, 4.0, 6.0))
    second = trimesh.creation.box(extents=(50.0, 50.0, 50.0))
    main = f"""<?xml version="1.0" encoding="UTF-8"?>
<model xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}" unit="millimeter" requiredextensions="p">
 <resources>
  <object id="1" type="model"><components><component objectid="1" p:path="/3D/Objects/object_1.model" /></components></object>
  <object id="2" type="model"><components><component objectid="1" p:path="/3D/Objects/object_2.model" /></components></object>
 </resources>
 <build>
  <item objectid="1" printable="1" transform="1 0 0 0 1 0 0 0 1 10 0 0" />
  <item objectid="2" printable="{'1' if second_printable else '0'}" transform="1 0 0 0 1 0 0 0 1 40 0 0" />
 </build>
</model>"""
    model_settings = """<?xml version="1.0" encoding="UTF-8"?>
<config>
 <object id="1">
  <metadata key="name" value="wanted-box" />
  <metadata key="enable_support" value="0" />
  <part id="1" subtype="normal_part">
   <metadata key="name" value="wanted-box" />
   <metadata key="source_file" value="wanted-box.stl" />
   <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1" />
   <mesh_stat face_count="12" />
  </part>
 </object>
 <object id="2">
  <metadata key="name" value="wrong-box" />
  <part id="1" subtype="normal_part"><mesh_stat face_count="12" /></part>
 </object>
 <plate>
  <model_instance><metadata key="object_id" value="1" /></model_instance>
  <model_instance><metadata key="object_id" value="2" /></model_instance>
 </plate>
 <assemble />
</config>"""
    settings = {
        "enable_support": "0",
        "support_type": "normal(auto)",
        "different_settings_to_system": [""],
        "printer_model": "Bambu Lab P1S",
        "printer_settings_id": "Bambu Lab P1S 0.4 nozzle",
        "printer_technology": "FFF",
        "nozzle_diameter": "0.4",
        "filament_type": ["PLA", "PLA"],
        "filament_colour": ["#000000", "#FF0000"],
        "curr_bed_type": "Textured PEI Plate",
        "printable_height": "250",
    }
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rel-1" Target="/3D/Objects/object_1.model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />
 <Relationship Id="rel-2" Target="/3D/Objects/object_2.model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" />
</Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", main)
        archive.writestr("3D/Objects/object_1.model", _mesh_part(first))
        archive.writestr("3D/Objects/object_2.model", _mesh_part(second))
        archive.writestr("3D/_rels/3dmodel.model.rels", rels)
        archive.writestr("Metadata/model_settings.config", model_settings)
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        archive.writestr(
            "Metadata/layer_config_ranges.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<objects><object id="1"><range min_z="0" max_z="3">
<option opt_key="extruder">2</option></range></object></objects>""",
        )
        archive.writestr("Metadata/filament_sequence.json", "{}")
        archive.writestr("Metadata/plate_1.png", b"stale preview")


class Project3MFTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.template = self.root / "template.3mf"
        _write_bambu_project(self.template)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_extracts_only_printable_geometry_and_applies_transform(self) -> None:
        output = self.root / "extracted.stl"

        result = extract_printable_stl(self.template, output)
        mesh = trimesh.load_mesh(output, process=False)

        self.assertEqual(result.printable_object_count, 1)
        self.assertEqual(result.object_names, ("wanted-box",))
        self.assertEqual(result.triangle_count, 12)
        self.assertAlmostEqual(float(mesh.bounds[0][0]), 9.0)
        self.assertAlmostEqual(float(mesh.bounds[1][0]), 11.0)
        self.assertLess(float(mesh.extents.max()), 7.0)

    def test_extracts_each_printable_object_without_merging_identity(self) -> None:
        source = self.root / "two-models.3mf"
        _write_bambu_project(source, second_printable=True)

        result = extract_printable_stl(source, self.root / "two-models.stl")

        self.assertEqual(result.printable_object_count, 2)
        self.assertEqual(len(result.objects), 2)
        self.assertEqual([item.object_id for item in result.objects], ["1", "2"])
        self.assertEqual([item.triangle_count for item in result.objects], [12, 12])
        first = trimesh.load_mesh(result.objects[0].output_stl_path, process=False)
        second = trimesh.load_mesh(result.objects[1].output_stl_path, process=False)
        self.assertLess(float(first.extents.max()), 7.0)
        self.assertAlmostEqual(float(second.extents.max()), 50.0)
        self.assertNotEqual(
            result.objects[0].geometry_signature,
            result.objects[1].geometry_signature,
        )

    def test_multi_object_project_embeds_independent_native_settings(self) -> None:
        source = self.root / "two-models.3mf"
        _write_bambu_project(source, second_printable=True)
        first_settings = PrintSettings(
            layer_height_mm=0.12,
            wall_loops=4,
            top_layers=6,
            bottom_layers=5,
            supports=False,
            brim=False,
            nozzle_temperature_c=220,
            bed_temperature_c=55,
            fan_percent=100,
            sparse_infill_percent=12,
            outer_wall_speed_mm_s=120,
        )
        second_settings = PrintSettings(
            layer_height_mm=0.24,
            wall_loops=2,
            top_layers=3,
            bottom_layers=3,
            supports=True,
            brim=True,
            nozzle_temperature_c=220,
            bed_temperature_c=55,
            fan_percent=100,
            sparse_infill_percent=28,
            outer_wall_speed_mm_s=210,
        )

        prepared = create_bambu_project_from_3mf_objects(
            source,
            self.template,
            self.root / "object-specific.3mf",
            object_configurations=(
                ObjectPrintConfiguration("1", "wanted-box", first_settings, "none"),
                ObjectPrintConfiguration("2", "wrong-box", second_settings, "tree"),
            ),
        )

        self.assertEqual(prepared.printable_object_count, 2)
        with zipfile.ZipFile(prepared.project_path) as archive:
            model_settings = ET.fromstring(
                archive.read("Metadata/model_settings.config")
            )
            objects = {
                item.get("id"): {
                    metadata.get("key"): metadata.get("value")
                    for metadata in item.findall("./metadata")
                }
                for item in model_settings.findall("./object")
            }
            main = ET.fromstring(archive.read("3D/3dmodel.model"))
            namespace = {"m": CORE_NS}
            transforms = [
                item.get("transform")
                for item in main.findall("./m:build/m:item", namespace)
            ]
        self.assertEqual(objects["1"]["layer_height"], "0.12")
        self.assertEqual(objects["1"]["wall_loops"], "4")
        self.assertEqual(objects["1"]["enable_support"], "0")
        self.assertEqual(objects["2"]["layer_height"], "0.24")
        self.assertEqual(objects["2"]["sparse_infill_density"], "28%")
        self.assertEqual(objects["2"]["outer_wall_speed"], "210")
        self.assertEqual(objects["2"]["support_type"], "tree(auto)")
        self.assertEqual(transforms[0], "1 0 0 0 1 0 0 0 1 10 0 0")
        self.assertEqual(transforms[1], "1 0 0 0 1 0 0 0 1 40 0 0")

    def test_disconnected_stl_models_become_independent_project_objects(self) -> None:
        first = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
        second = trimesh.creation.icosphere(subdivisions=2, radius=8.0)
        second.apply_translation((40.0, 0.0, 8.0))
        source = self.root / "two-bodies.stl"
        trimesh.util.concatenate((first, second)).export(source)
        extracted = extract_stl_objects(source, self.root / "two-bodies-combined.stl")
        settings = PrintSettings(
            layer_height_mm=0.2,
            wall_loops=3,
            top_layers=4,
            bottom_layers=4,
            supports=False,
            brim=False,
            nozzle_temperature_c=220,
            bed_temperature_c=55,
            fan_percent=100,
        )

        prepared = create_bambu_project_from_stl_objects(
            extracted,
            self.template,
            self.root / "two-bodies.3mf",
            object_configurations=tuple(
                ObjectPrintConfiguration(item.object_id, item.name, settings, "none")
                for item in extracted.objects
            ),
        )

        self.assertEqual(extracted.printable_object_count, 2)
        self.assertEqual(prepared.printable_object_count, 2)
        with zipfile.ZipFile(prepared.project_path) as archive:
            main = ET.fromstring(archive.read("3D/3dmodel.model"))
            model_settings = ET.fromstring(
                archive.read("Metadata/model_settings.config")
            )
        namespace = {"m": CORE_NS}
        self.assertEqual(len(main.findall("./m:build/m:item", namespace)), 2)
        self.assertEqual(len(model_settings.findall("./object")), 2)

    def test_prepared_project_replaces_template_geometry_and_name(self) -> None:
        source = self.root / "keycap.stl"
        trimesh.creation.icosphere(subdivisions=2, radius=7.0).export(source)

        prepared = create_bambu_project_from_stl(
            source,
            self.template,
            self.root / "prepared.3mf",
            support_mode="tree",
            recommended_settings=PrintSettings(
                layer_height_mm=0.16,
                wall_loops=3,
                top_layers=5,
                bottom_layers=4,
                supports=True,
                brim=True,
                nozzle_temperature_c=220,
                bed_temperature_c=55,
                fan_percent=100,
                seam_position="back",
                scarf_seam_type="external",
                override_filament_scarf_seam=True,
                small_perimeter_speed_percent=50,
                small_perimeter_threshold_mm=5.0,
                slow_down_layer_time_s=12,
                slow_down_min_speed_mm_s=20,
                initial_layer_height_mm=0.24,
                line_width_mm=0.44,
                ironing_enabled=True,
            ),
        )
        extracted = extract_printable_stl(
            prepared.project_path, self.root / "prepared-extracted.stl"
        )

        self.assertEqual(extracted.printable_object_count, 1)
        self.assertEqual(extracted.object_names, (source.name,))
        self.assertEqual(extracted.triangle_count, prepared.triangle_count)
        with zipfile.ZipFile(prepared.project_path) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertNotIn("3D/Objects/object_2.model", archive.namelist())
            self.assertNotIn("Metadata/layer_config_ranges.xml", archive.namelist())
            self.assertNotIn("Metadata/filament_sequence.json", archive.namelist())
            self.assertNotIn("Metadata/plate_1.png", archive.namelist())
            model_settings = ET.fromstring(
                archive.read("Metadata/model_settings.config")
            )
            extruders = [
                item.get("value")
                for item in model_settings.findall(".//metadata")
                if item.get("key") == "extruder"
            ]
            self.assertEqual(extruders, ["1"])
        self.assertEqual(settings["enable_support"], "1")
        self.assertEqual(settings["support_type"], "tree(auto)")
        self.assertEqual(settings["layer_height"], "0.16")
        self.assertEqual(settings["initial_layer_print_height"], "0.24")
        self.assertEqual(settings["line_width"], "0.44")
        self.assertEqual(settings["ironing_type"], "topmost")
        self.assertEqual(settings["wall_loops"], "3")
        self.assertEqual(settings["bottom_shell_layers"], "4")
        self.assertEqual(settings["seam_position"], "back")
        self.assertEqual(settings["seam_slope_type"], "external")
        self.assertEqual(settings["override_filament_scarf_seam_setting"], "1")
        self.assertEqual(settings["small_perimeter_speed"], "50%")
        self.assertEqual(settings["small_perimeter_threshold"], "5")
        self.assertEqual(settings["slow_down_layer_time"], "12")
        self.assertEqual(settings["slow_down_min_speed"], "20")

    def test_native_local_modifier_ranges_are_written_and_verified(self) -> None:
        source = self.root / "local.stl"
        trimesh.creation.box(extents=(10, 10, 12)).export(source)
        plan = LocalModifierPlan(
            1,
            "height-ranges",
            (
                LocalModifierRange(1, 0.0, 6.0, "hidden-efficient", {"inner_wall_speed": "340"}, "HIGH", "safe"),
                LocalModifierRange(2, 6.0, 12.0, "top-protected", {"top_surface_speed": "70", "top_shell_layers": "6"}, "HIGH", "visible"),
            ),
            0.5,
            0.5,
            (),
        )
        prepared = create_bambu_project_from_stl(
            source,
            self.template,
            self.root / "local.3mf",
            support_mode="none",
            local_modifier_plan=plan,
        )
        with zipfile.ZipFile(prepared.project_path) as archive:
            root = ET.fromstring(archive.read("Metadata/layer_config_ranges.xml"))
            self.assertEqual(len(root.findall("./object/range")), 2)
            self.assertEqual(root.find("./object/range/option").get("opt_key"), "inner_wall_speed")
        self.assertEqual(prepared.local_modifier_range_count, 2)

    def test_local_modifier_ranges_above_100_mm_keep_verification_precision(self) -> None:
        source = self.root / "tall-local.stl"
        trimesh.creation.box(extents=(10, 10, 128)).export(source)
        plan = LocalModifierPlan(
            1,
            "height-ranges",
            (
                LocalModifierRange(
                    1,
                    99.314,
                    106.4078,
                    "bridge-protected",
                    {"layer_height": "0.16", "bridge_speed": "35"},
                    "HIGH",
                    "bridge",
                ),
                LocalModifierRange(
                    2,
                    106.4078,
                    127.6894,
                    "detail-protected",
                    {"layer_height": "0.16", "outer_wall_speed": "90"},
                    "HIGH",
                    "detail",
                ),
            ),
            0.5,
            0.5,
            (),
        )
        prepared = create_bambu_project_from_stl(
            source,
            self.template,
            self.root / "tall-local.3mf",
            support_mode="none",
            local_modifier_plan=plan,
        )
        gcode = self.root / "tall-local.gcode"
        gcode.write_bytes(b"; test gcode\n")
        with zipfile.ZipFile(prepared.project_path, "a") as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode.read_bytes())

        verification = verify_ready_project(
            prepared.project_path,
            expected_geometry_path=source,
            expected_local_modifier_plan=plan,
            expected_printable_count=1,
            expected_single_color=True,
            external_gcode=gcode,
        )

        self.assertTrue(verification.valid, verification.errors)

    def test_single_color_verification_rejects_inherited_layer_assignment(self) -> None:
        source = self.root / "single-color.stl"
        trimesh.creation.box().export(source)
        prepared = create_bambu_project_from_stl(
            source,
            self.template,
            self.root / "color-leak.3mf",
            support_mode="none",
        )
        gcode = self.root / "plate_1.gcode"
        gcode.write_bytes(b"; test gcode\n")
        with zipfile.ZipFile(prepared.project_path, "a") as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode.read_bytes())
            archive.writestr(
                "Metadata/layer_config_ranges.xml",
                """<objects><object id="1"><range min_z="0" max_z="3">
<option opt_key="extruder">2</option></range></object></objects>""",
            )

        verification = verify_ready_project(
            prepared.project_path,
            expected_geometry_path=source,
            expected_single_color=True,
            external_gcode=gcode,
        )

        self.assertFalse(verification.valid)
        self.assertTrue(
            any("layer-range color" in error for error in verification.errors),
            verification.errors,
        )

    def test_ready_verification_rejects_different_same_face_count_model(self) -> None:
        source = self.root / "model.stl"
        trimesh.creation.box(extents=(2.0, 4.0, 6.0)).export(source)
        prepared = create_bambu_project_from_stl(
            source,
            self.template,
            self.root / "ready.3mf",
            support_mode="none",
        )
        gcode = self.root / "plate_1.gcode"
        gcode.write_bytes(b"; verified gcode\n")
        with zipfile.ZipFile(prepared.project_path, "a") as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode.read_bytes())

        valid = verify_ready_project(
            prepared.project_path,
            expected_object_name=source.name,
            expected_triangle_count=prepared.triangle_count,
            expected_geometry_path=source,
            expected_support_mode="none",
            external_gcode=gcode,
        )
        self.assertTrue(valid.valid, valid.errors)

        wrong = self.root / "wrong.stl"
        trimesh.creation.box(extents=(3.0, 3.0, 3.0)).export(wrong)
        wrong_prepared = create_bambu_project_from_stl(
            wrong,
            self.template,
            self.root / "wrong-ready.3mf",
            support_mode="none",
        )
        with zipfile.ZipFile(wrong_prepared.project_path, "a") as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode.read_bytes())
        rejected = verify_ready_project(
            wrong_prepared.project_path,
            expected_triangle_count=prepared.triangle_count,
            expected_geometry_path=source,
            expected_support_mode="none",
            external_gcode=gcode,
        )
        self.assertFalse(rejected.valid)
        self.assertTrue(any("fingerprint" in error for error in rejected.errors))

    def test_geometry_verification_accepts_large_float32_arrangement(self) -> None:
        from ai_print_optimizer.project3mf import _same_geometry

        large = trimesh.creation.icosphere(subdivisions=4, radius=80.0)
        small = trimesh.creation.icosphere(subdivisions=2, radius=6.0)
        small.apply_translation((100.0, 100.0, -60.0))
        expected = trimesh.util.concatenate((large, small))

        arranged = expected.copy()
        transform = trimesh.transformations.rotation_matrix(
            np.deg2rad(37.0), (0.0, 0.0, 1.0)
        )
        transform[:3, 3] = (128.0, 117.0, 60.0)
        arranged.apply_transform(transform)
        arranged.vertices = np.asarray(arranged.vertices, dtype=np.float32).astype(
            np.float64
        )

        self.assertTrue(_same_geometry(expected, arranged))

        changed = arranged.copy()
        changed.vertices[0, 0] += 0.01
        self.assertFalse(_same_geometry(expected, changed))


if __name__ == "__main__":
    unittest.main()
