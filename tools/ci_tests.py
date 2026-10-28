"""Run the test suite in CI and report each failure as a GitHub annotation."""
import io
import sys
import unittest


def escape(text):
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def main():
    stream = io.StringIO()
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(stream.getvalue())
    for test, trace in (result.errors + result.failures)[:10]:
        print(f"::error title={escape(str(test))[:200]}::{escape(trace[-3500:])}")
    if result.skipped:
        print("::warning title=Preskočeni testovi::" + escape("\n".join(f"{t}: {why}" for t, why in result.skipped)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
