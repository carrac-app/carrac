"""Download the curl-chart renderer into the image at build time.

The plain-text chart endpoint (``curl <host>/btc``) shells out to the
``render`` subcommand of the carrac-tui binary, a separate Rust project
(github.com/carrac-app/carrac-tui). Building Rust in this image would add a
toolchain for one subcommand, so the Dockerfile fetches a pinned release
archive instead, checks it against the SHA-256 pinned here, and installs the
binary at ``bin/carrac-render`` (the default ``CURL_RENDER_BIN``). The
archive's licence files go to ``/usr/share/doc/carrac-tui/``.

Usage: python fetch-tui-renderer.py <docker TARGETARCH> <destination>
"""

import hashlib
import io
import os
import subprocess
import sys
import tarfile
import urllib.request

VERSION = '1.0.2'
RELEASES = 'https://github.com/carrac-app/carrac-tui/releases/download'
# docker TARGETARCH -> (Rust target, SHA-256 of the release archive)
ARCHIVES = {
    'amd64': ('x86_64-unknown-linux-gnu',
              'd87ecd4b6a30274c87a549f26b0518c38ce372ac536bb6215c2b0f6f6162f5af'),
    'arm64': ('aarch64-unknown-linux-gnu',
              'ba6354960543d6330a8fe65051851b573e11585a4ff9571086700b704d939ec1'),
}
LICENCES = ('LICENSE-MIT', 'LICENSE-APACHE', 'OFL.txt')
DOC_DIR = '/usr/share/doc/carrac-tui'


def main(arch: str, dest: str) -> None:
    if arch not in ARCHIVES:
        sys.exit(f'no carrac-tui build for TARGETARCH={arch!r}; '
                 f'supported: {", ".join(ARCHIVES)}')
    target, want = ARCHIVES[arch]
    name = f'carrac-tui-{target}'
    url = f'{RELEASES}/v{VERSION}/{name}.tar.xz'
    with urllib.request.urlopen(url, timeout=60) as resp:
        blob = resp.read()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        sys.exit(f'{url}: SHA-256 {got} does not match the pinned {want}')

    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:xz') as tar:
        def read(member: str) -> bytes:
            return tar.extractfile(f'{name}/{member}').read()

        os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(read('carrac'))
        os.chmod(dest, 0o755)
        os.makedirs(DOC_DIR, exist_ok=True)
        for lic in LICENCES:
            with open(os.path.join(DOC_DIR, lic), 'wb') as f:
                f.write(read(lic))

    # Fail the build here, not on the first curl request, if it cannot run.
    subprocess.run([dest, 'render', '--help'], check=True, stdout=subprocess.DEVNULL)
    print(f'carrac-tui v{VERSION} ({target}) installed at {dest}')


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1] or 'amd64', sys.argv[2])
