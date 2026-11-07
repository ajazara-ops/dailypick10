"""Local Auth/Firestore only. Not a worker CLI and not wired to any workflow."""
import os
from pathlib import Path
import subprocess
import sys
from emulator_only import EmulatorGuard

def main():
    if os.environ.get('DAILYPICK10_SPARK_TEST')!='1':raise RuntimeError('Explicit synthetic test required')
    EmulatorGuard().preflight()
    root=Path(__file__).resolve().parents[1]
    if os.environ.get('DAILYPICK10_SPARK_BRIDGE_ONLY')!='1':
        suite='spark-transport.test.cjs' if os.environ.get('DAILYPICK10_SPARK_TRANSPORT_ONLY')=='1' else 'spark-intake.test.cjs'
        result=subprocess.run(['node','--test','--test-concurrency=1','emulator-tests/'+suite],cwd=root/'mobile')
        if result.returncode:return result.returncode
    return subprocess.run([sys.executable,'-m','unittest','discover','-s','emulator_tests','-p','test_spark_intake.py','-v'],cwd=root/'server').returncode

if __name__=='__main__':raise SystemExit(main())
