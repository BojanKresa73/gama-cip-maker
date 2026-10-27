"""Builds a realistic test plate PDF: CMYK blocks, a Pantone area and a ŠTANC (die-cut) line."""
import sys


def build(path, w_mm=1060, h_mm=795):
    k = 72 / 25.4
    W, H = w_mm * k, h_mm * k
    ops = []
    # Four 'boxes' with different CMYK mixes across the sheet
    boxes = [(60, 120, 220, 300, "0.8 0.1 0 0"), (320, 120, 220, 300, "0 0.7 0.9 0"), (580, 120, 200, 300, "0.1 0.1 0.9 0"),
             (820, 120, 160, 300, "0 0 0 1"), (60, 470, 460, 200, "0.5 0.5 0.5 0.5")]
    for x, y, w, h, cmyk in boxes:
        ops.append(f"{cmyk} k {x*k:.2f} {(h_mm-y-h)*k:.2f} {w*k:.2f} {h*k:.2f} re f")
    # Gradient-like strip in black, 10 steps
    for i in range(10):
        ops.append(f"0 0 0 {i/10:.1f} k {(560+i*40)*k:.2f} {(h_mm-670)*k:.2f} {40*k:.2f} {200*k:.2f} re f")
    # Pantone spot area and ŠTANC outline
    ops.append(f"/P cs 1 scn {600*k:.2f} {(h_mm-460)*k:.2f} {380*k:.2f} {60*k:.2f} re f")
    ops.append(f"/S CS 1 SCN 2 w {50*k:.2f} {(h_mm-690)*k:.2f} {940*k:.2f} {590*k:.2f} re S")
    stream = "\n".join(ops).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W:.3f} {H:.3f}] /Resources << /ColorSpace << /P [/Separation /PANTONE#20871#20C /DeviceCMYK 5 0 R] /S [/Separation /#C5#A0TANC /DeviceCMYK 6 0 R] >> >> /Contents 4 0 R >>".encode(),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0.2 0.3 0.6 0.25] /N 1 >>",
            b"<< /FunctionType 2 /Domain [0 1] /C0 [0 0 0 0] /C1 [0 1 0 0] /N 1 >>"]
    data = b"%PDF-1.7\n"; offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(data)); data += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    x = len(data); data += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for o in offs: data += f"{o:010d} 00000 n \n".encode()
    data += f"trailer << /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    open(path, "wb").write(data)


if __name__ == "__main__":
    build(sys.argv[1])
