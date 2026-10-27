# Gama United · CIP Maker (lokalni program)

Program za Windows koji iz PDF-a štamparskog tabaka računa pokrivenost boje po zonama
i pravi fajlove za mašinu. Radi potpuno lokalno, bez interneta i bez naloga.

## Šta radi

- Rasterizuje PDF pravim separacijama (Ghostscript, 600 → 150 dpi): CMYK i sve spot boje,
  uključujući imena sa slovima Š, Č, Ć, Ž, Đ.
- Prikazuje pregled ploče sa mrežom zona, sličice separacija i grafik pokrivenosti po zonama.
- Separacije dorade (ŠTANC, BIG, PERFORACIJA) su automatski isključene iz izvoza.
- Izlazi, po profilu mašine:
  - **XML + PNG** (JDF/JMF, DI-Plot dijalekt) — npr. Birograf.
  - **PPF** (CIP3 2.1, isti raspored kao PressPerCent) — npr. Eurodream.
- Profili mašina se uređuju u programu. Svaka izmena geometrije briše oznaku provere.

## Instalacija

Preuzmite `GamaCIPMaker-Setup-x.y.z.exe` iz GitHub Actions (Artifacts) ili Releases,
pokrenite ga i pratite korake. Administratorska prava nisu potrebna.
Potreban je Windows 10 ili 11 (Microsoft WebView2 je već deo sistema).

Podaci (profili i poslednjih 50 poslova) čuvaju se u `%LOCALAPPDATA%\GamaCIP`.

## Pravljenje instalera

Svaki `push` na granu `main` pokreće `.github/workflows/build-windows.yml`, koji:
1. instalira Python biblioteke i Ghostscript,
2. pokreće sve testove na Windows-u,
3. pravi program PyInstaller-om sa ugrađenim Ghostscript-om,
4. pokreće samoproveru gotovog `.exe` (obrada + XML + PPF),
5. pravi instaler Inno Setup-om.

Tag `v1.0.0` (i slični) dodatno objavljuje instaler u Releases.

## Razvoj (Linux/macOS/Windows)

```
pip install -r requirements.txt
python run_gama_cip.py            # prozor programa
python run_gama_cip.py --browser  # isto, u pregledaču
PYTHONPATH=.:tests python -m unittest discover -s tests
```

## Važne napomene

- Profili su **upoređeni sa uzorcima**, ali nijedan još nije proveren učitavanjem u konzolu mašine.
- Ghostscript i PyMuPDF su pod AGPL licencom. Za internu upotrebu u Gama United to je u redu;
  pre deljenja programa drugim firmama proverite licencu (vidi `packaging/LICENSES.txt`).
