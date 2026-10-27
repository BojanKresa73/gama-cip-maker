"""Real 600 -> 150 dpi separation and export, with analytic CMYK + spot areas."""
import json
import shutil
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from gama_cip.engine import ghostscript, process_pdf
from gama_cip.exporter import export_zip
from gama_cip.profiles import ROOT


def vector_pdf(path):
    # 100 mm square. Left 50 mm black; right 50 mm named spot at 50% tint.
    pts=100*72/25.4; half=pts/2
    stream=f"0 0 0 1 k 0 0 {half} {pts} re f /Spot cs 0.5 scn {half} 0 {half} {pts} re f".encode()
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
      f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {pts} {pts}] /Resources << /ColorSpace << /Spot [/Separation /PANTONE#207466C /DeviceCMYK 5 0 R] >> >> /Contents 4 0 R >>'.encode(),
      b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream',
      b'<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [1 0 0 0] /N 1 >>']
    data=b'%PDF-1.7\n';offsets=[0]
    for i,obj in enumerate(objects,1):
        offsets.append(len(data));data+=f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n'
    xref=len(data);data+=f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]:data+=f'{offset:010d} 00000 n \n'.encode()
    data+=f'trailer << /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(data)


def _has_gs():
    try:
        return bool(ghostscript())
    except ValueError:
        return False


@unittest.skipUnless(_has_gs(), 'Ghostscript required')
class NativeRaster(unittest.TestCase):
    def test_native_spot_planes_geometry_and_xml_export(self):
        p=json.loads(next((ROOT/'profiles').glob('*.json')).read_text())
        p.update(plate_width_mm=100,plate_height_mm=100,zone_count=2,zone_width_mm=50,
                 area_height_mm=100,top_mm=0,shift_x_mm=0,render_dpi=600,preview_dpi=50)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);pdf=root/'analytic.pdf';vector_pdf(pdf)
            result=process_pdf(pdf,p,root/'result')
            page=result['pages'][0];channels={c['name']:c for c in page['channels']}
            self.assertIn('Black',channels);self.assertIn('PANTONE 7466C',channels)
            self.assertAlmostEqual(channels['Black']['values'][0],1,delta=.005)
            self.assertAlmostEqual(channels['Black']['values'][1],0,delta=.005)
            self.assertAlmostEqual(channels['PANTONE 7466C']['values'][0],0,delta=.005)
            self.assertAlmostEqual(channels['PANTONE 7466C']['values'][1],.5,delta=.005)
            self.assertEqual(result['render_dpi'],600);self.assertEqual(result['coverage_dpi'],150)
            archive=export_zip(result,root/'result',{'1':['Black','PANTONE 7466C']},'TEST_600','Front','analytic.pdf')
            with zipfile.ZipFile(archive) as z:
                xml=ET.fromstring(z.read('analytic_0001.xml'))
                records=[node for node in xml.iter() if 'ZoneSettingsX' in node.attrib]
                self.assertEqual(len(records),2)
                self.assertTrue(z.read('analytic_0001.PNG').startswith(b'\x89PNG'))
                self.assertIn('izvestaj/page-0001.render.log',z.namelist())


if __name__=='__main__':unittest.main()
