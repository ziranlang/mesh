import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    binary = sys.argv[1]
    public_key = "MESH_TEST_MLDSA_PUBLIC_KEY"
    signature = "MESH_TEST_MLDSA_SIGNATURE"
    message = "MESH_TEST_MLDSA_MESSAGE"
    environment = {key: value for key, value in os.environ.items()
                   if key not in (public_key, signature, message)}
    subprocess.run([binary], env=environment, check=True, timeout=5)

    fixture = Path(__file__).resolve().parents[2] / "testdata/ml_dsa44_wycheproof.json"
    cases = json.loads(fixture.read_text())
    seen = []
    for group in cases["testGroups"]:
        for case in group["tests"]:
            inputs = {public_key: group["publicKey"], signature: case["sig"],
                      message: case["msg"]}
            result = subprocess.run([binary], env=environment | inputs, timeout=5)
            expected = 0 if case["result"] == "valid" else 1
            if result.returncode != expected:
                raise AssertionError(f"ML-DSA-44 case {case['tcId']} exited with "
                                     f"{result.returncode}, expected {expected}")
            seen.append(case["tcId"])
    if seen != [1, 8, 15, 64, 65]:
        raise AssertionError(f"Unexpected ML-DSA-44 case IDs {seen}")


if __name__ == "__main__":
    main()
