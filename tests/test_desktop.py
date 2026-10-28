"""End-to-end test of the local program API: profile -> PDF -> result -> ZIP."""
import json
import shutil
import tempfile
import time
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from gama_cip_desktop.server import start
from test_native_integration import vector_pdf


class Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app, self.server, url = start(self.tmp.name)
        self.base = url.split("/?")[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, raw=None, token=True):
        headers = {"X-Token": self.app.token} if token else {}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(Request(self.base + path, data=data, method=method, headers=headers)) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if r.headers.get_content_type() == "application/json" else payload)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait(self, job_id):
        for _ in range(300):
            status, body = self.call("GET", f"/api/jobs/{job_id}")
            if body["job"]["status"] in ("completed", "failed"):
                return body["job"]
            time.sleep(0.1)
        self.fail("timeout")

    def test_token_required(self):
        self.assertEqual(self.call("GET", "/api/profiles", token=False)[0], 403)

    def test_seed_profiles_and_blockers(self):
        profiles = {p["id"]: p for p in self.call("GET", "/api/profiles")[1]["profiles"]}
        self.assertEqual(len(profiles), 5)
        self.assertTrue(profiles["eurodream-745x605"]["usable"])
        self.assertTrue(profiles["birograf-rapida105-1060x795"]["usable"])
        self.assertFalse(profiles["unagraf-745x605"]["usable"])
        self.assertIn("Nedostaje: Mašina", profiles["unagraf-745x605"]["blockers"])

    def test_edit_profile_resets_verification(self):
        p = self.call("GET", "/api/profiles")[1]["profiles"]
        bir = next(x for x in p if x["id"].startswith("birograf"))
        bir["top_mm"] = 20
        saved = self.call("POST", "/api/profiles", {"profile": bir, "original_id": bir["id"]})[1]["profile"]
        self.assertEqual(saved["validation_status"], "unverified")
        same = self.call("POST", "/api/profiles", {"profile": saved, "original_id": saved["id"]})[1]["profile"]
        self.assertEqual(same["validation_status"], "unverified")

    def test_full_flow_with_spot_and_serbian_name(self):
        p = self.call("GET", "/api/profiles")[1]["profiles"]
        test = next(x for x in p if x["id"].startswith("birograf"))
        test.update(id="test-100", name="Test 100 mm", plate_width_mm=100, plate_height_mm=100, zone_count=2,
                    zone_width_mm=50, area_height_mm=100, top_mm=0, preview_dpi=50, active=True, export_enabled=True)
        status, body = self.call("POST", "/api/profiles", {"profile": test})
        self.assertEqual(status, 200, body)
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "a.pdf"
            vector_pdf(pdf)
            data = pdf.read_bytes().replace(b"/PANTONE#207466C", b"/#C5#A0TANC")
        status, body = self.call("POST", "/api/jobs?profile=test-100&name=Kutija%20%C5%A1tanc.pdf", raw=data)
        self.assertEqual(status, 201, body)
        job = self.wait(body["job"]["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        channels = {c["name"]: c for c in job["result"]["pages"][0]["channels"]}
        self.assertAlmostEqual(channels["ŠTANC"]["values"][1], .5, delta=.005)
        thumb = channels["Black"]["thumbnail"]
        status, png = self.call("GET", f"/api/jobs/{job['id']}/files/{thumb}?t={self.app.token}", token=False)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(self.call("GET", f"/api/jobs/{job['id']}/files/source.pdf")[0], 404)
        status, exp = self.call("POST", f"/api/jobs/{job['id']}/export", {"selections": {"1": ["Black"]}, "job_id": "K_99", "side": "Front"})
        self.assertEqual(status, 200, exp)
        status, z = self.call("GET", f"/api/jobs/{job['id']}/download/{exp['name']}")
        with zipfile.ZipFile(BytesIO(z)) as archive:
            names = archive.namelist()
            self.assertIn("Kutija_stanc_0001.xml", names)
            self.assertIn(b"K_99", archive.read("Kutija_stanc_0001.xml"))

    def test_rejects_non_pdf_and_incomplete_profile(self):
        self.assertEqual(self.call("POST", "/api/jobs?profile=birograf-rapida105-1060x795&name=x.pdf", raw=b"hello")[0], 400)
        status, body = self.call("POST", "/api/jobs?profile=unagraf-745x605&name=x.pdf", raw=b"%PDF-1.4 x")
        self.assertEqual(status, 400)
        self.assertIn("nije spreman", body["error"])

    def test_wrong_format_fails_with_clear_message(self):
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "a.pdf"
            vector_pdf(pdf)
            data = pdf.read_bytes()
        status, body = self.call("POST", "/api/jobs?profile=birograf-rapida105-1060x795&name=a.pdf", raw=data)
        job = self.wait(body["job"]["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("profil očekuje", job["error"])


if __name__ == "__main__":
    unittest.main()


class BridgeExposure(unittest.TestCase):
    def test_bridge_exposes_only_methods(self):
        """pywebview walks public attributes recursively; anything non-callable here froze the program on Windows."""
        import inspect
        from gama_cip_desktop.main import Bridge

        bridge = Bridge(object())
        bridge._window = object()
        public = {n: getattr(bridge, n) for n in dir(bridge) if not n.startswith("_")}
        self.assertEqual(sorted(public), ["choose_folder", "open_data_folder", "open_path", "save_export", "show_in_folder"])
        self.assertTrue(all(inspect.ismethod(v) for v in public.values()))
