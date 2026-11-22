"""Write the native entry module with the selected build version."""

import json
import os
from pathlib import Path
import sys


def main():
    version = os.environ["MESH_NATIVE_BUILD_VERSION"]
    if not version or not version.isprintable():
        raise SystemExit("Mesh native build version must be nonempty printable text")
    output = Path(sys.argv[1])
    literal = json.dumps(version, ensure_ascii=False)
    output.write_text(
        'server :: #import "server_native";\n'
        'version :: #import "version";\n\n'
        '#program_export\n'
        'Main :: (argc: s32, argv: **u8) -> s32 {\n'
        f'    version.BuildVersion = {literal}\n'
        '    return server.Main(argc, argv)\n'
        '}\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
