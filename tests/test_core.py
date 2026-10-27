import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from gama_cip.engine import coverage, orient_array, orient_image, make_preview, placement
from gama_cip.exporter import NS, export_zip, xml_bytes
from gama_cip.profiles import ROOT, ProfileStore, validate


def profile():
    return json.loads(next((ROOT / "profiles").glob("*.json")).read_text(encoding="utf-8"))


class Geometry(unittest.TestCase):
    def setUp(self):
        self.p = profile()
        self.p.update(plate_width_mm=60, plate_height_mm=40, zone_count=2, zone_width_mm=30, area_height_mm=40, top_mm=0)

    def test_solid_and_blank(self):
        np.testing.assert_allclose(coverage(np.ones((40, 60)), [60, 40], self.p), [1, 1])
        np.testing.assert_allclose(coverage(np.zeros((40, 60)), [60, 40], self.p), [0, 0])

    def test_white_padding_uses_full_denominator(self):
        self.p["top_mm"] = 10
        np.testing.assert_allclose(coverage(np.ones((40, 60)), [60, 40], self.p), [.75, .75])

    def test_fractional_pixel_is_integrated(self):
        self.p["shift_x_mm"] = 15
        np.testing.assert_allclose(coverage(np.array([[1., 0.]]), [60, 40], self.p), [.5, .5])

    def test_band_and_reverse(self):
        a = np.zeros((40, 60)); a[:, :30] = 1
        np.testing.assert_allclose(coverage(a, [60, 40], self.p), [1, 0])
        self.p["reverse_zones"] = True
        np.testing.assert_allclose(coverage(a, [60, 40], self.p), [0, 1])

    def test_rotation_and_mirrors_match_preview(self):
        a = np.arange(6, dtype=np.uint8).reshape(2, 3)
        for rotation in [0, 90, 180, 270]:
            for mx in (True, False):
                for my in (True, False):
                    self.p.update(rotation=rotation, mirror_x=mx, mirror_y=my)
                    np.testing.assert_array_equal(orient_array(a, self.p), np.array(orient_image(Image.fromarray(a), self.p)))

    def test_preview_dimensions_and_padding(self):
        self.p["top_mm"] = 10
        im = make_preview(Image.new("RGB", (600, 400), "black"), [60, 40], self.p, 25)
        self.assertEqual(im.size, (59, 39))
        self.assertEqual(im.getpixel((30, 1)), (255, 255, 255))
        self.assertEqual(im.getpixel((30, 25)), (0, 0, 0))

    def test_sheet_bottom_anchor_changes_with_actual_input_size(self):
        p=profile()
        p.update(zone_count=23,zone_width_mm=32.5,area_height_mm=520,vertical_anchor="bottom",bottom_mm=30)
        self.assertEqual(placement([640,440],p),(53.75,110))
        self.assertEqual(placement([700,500],p),(23.75,50))
        self.assertEqual(placement([745,605],p),(1.25,-55))


class Profiles(unittest.TestCase):
    def test_invalid_geometry_rejected(self):
        for key, value in [("zone_count", 0), ("zone_count", True), ("zone_width_mm", float("nan")), ("rotation", 12), ("render_dpi", 600.0), ("schema_version", 2), ("id", "../bad"), ("shift_x_mm", float("inf"))]:
            p=profile();p[key]=value
            with self.assertRaises(ValueError):validate(p)

    def test_edited_or_copied_profile_loses_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=ProfileStore(tmp)
            p=store.all()[0]
            self.assertEqual(p["validation_status"], "reference_compared")
            p["top_mm"]=20
            self.assertEqual(store.save(p)["validation_status"], "unverified")
            p["id"]="new-printer";p["validation_status"]="reference_compared"
            self.assertEqual(store.save(p)["validation_status"], "unverified")
            self.assertEqual(len(store.all()), 2)


class Export(unittest.TestCase):
    def setUp(self):
        self.p=profile()
        self.channel={"name": 'Spot & "test"', "values": [.123456]*35, "rgb": [1, .5, 0]}
        self.page={"page":1,"png":"x.PNG","thumbnail":"thumb.png","preview_size":[6201,4606],"channels":[self.channel]}

    def test_xml_values_and_names(self):
        data=xml_bytes(self.page,self.p,[self.channel],'J & "A"','Back','S1','x.PNG')
        root=ET.fromstring(data);ns={"j":NS}
        ink=root.find('.//j:InkZoneProfile[@Separation]',ns)
        self.assertEqual(ink.attrib["Separation"],self.channel["name"])
        self.assertEqual(ink.attrib["ZoneSettingsX"].split(),['0.123']*35)
        self.assertEqual(root.find('.//j:Preview[@Size]',ns).attrib["Size"],"6201 4606")
        self.assertEqual(root.find('.//j:Preview[@Side]',ns).attrib["Side"],"Back")
        with self.assertRaises(ValueError):
            xml_bytes(self.page,self.p,[self.channel],'Invalid\x00','Front','S1','x.PNG')

    def test_selection_and_export_package(self):
        other=dict(self.channel,name="Black")
        self.page["channels"].append(other)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            Image.new("RGB",(5,5),"white").save(root/"x.PNG")
            Image.new("RGB",(2,2),"white").save(root/"thumb.png")
            result={"profile":self.p,"pages":[self.page]}
            path=export_zip(result,root,{"1":["Black"]},"J1","Front","example.pdf")
            with zipfile.ZipFile(path) as z:
                xml=ET.fromstring(z.read("example_0001.xml"))
                self.assertEqual([n.attrib['Separation'] for n in xml.iter() if 'Separation' in n.attrib],["Black"])
                self.assertIn("example_0001.PNG",z.namelist())
            with self.assertRaises(ValueError):export_zip(result,root,{"1":[]},"J1","Front","x.pdf")
            with self.assertRaises(ValueError):export_zip(result,root,{"1":["Missing"]},"J1","Front","x.pdf")


if __name__ == "__main__":
    unittest.main()
