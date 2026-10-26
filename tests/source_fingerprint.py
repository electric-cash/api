"""Identify Python-only source trees consistently across local validation tools."""
import hashlib


def source_digest(root):
    if not ((root / 'api.py').is_file() and (root / 'daemon.py').is_file()):
        raise RuntimeError('Both reviewed source trees must be mounted')
    paths = list(root.rglob('*'))
    if root.is_symlink() or any(path.is_symlink() for path in paths):
        raise RuntimeError('Symlink in source tree')
    if any(path.suffix.lower() in ('.pyc', '.pyo', '.so', '.pyd') for path in paths):
        raise RuntimeError('Compiled artifacts are not covered by the source fingerprint')
    digest = hashlib.sha256()
    for path in sorted((p for p in paths if p.suffix == '.py'),
                       key=lambda p: p.relative_to(root).as_posix()):
        digest.update(path.relative_to(root).as_posix().encode() + b'\0')
        digest.update(hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).digest())
    return digest.hexdigest()
